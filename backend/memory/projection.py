"""M5-01: Projection / Claim 地基（来源无关、可重算）。

原则：
- records_v1 是证据层，投影层只读证据，绝不覆盖、合并、删除证据；
- Claim 是可版本化、可重算的记忆表达；同一 claim_id 的每次来源变化
  产生新版本，旧版本置为 superseded，历史留痕不删除；
- claim_evidence 保存 Claim ↔ 证据的多对多关联，一条证据可投影成
  多条 Claim，多条证据可汇成一条 Claim，完整来源可追溯；
- agent_id 归属明确且不可绕行：所有接口要求非空 agent_id，
  不提供 None 通道；
- build_projection 幂等：同一 (rule, agent, 证据集) 重复构建不产生
  重复行、不推进版本号。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .records_v1 import migrate_records_db, _connect, _resolve_db_path

PROJECTION_SCHEMA_VERSION = "echo-pact-claims-v1"
CLAIM_KINDS = ("fact", "preference", "relationship", "task", "note")
CLAIM_STATUSES = ("active", "superseded", "sealed")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    """Serialize identity inputs without delimiter ambiguity."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_hash(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")
    return value.strip()


def _require_agent_id(agent_id: Optional[str]) -> str:
    """agent 归属不可绕行：空值、None、空白一律拒绝。"""
    try:
        return _require_non_empty_string(agent_id, "agent_id")
    except ValueError as exc:
        raise ValueError(
            "agent_id 必须是非空字符串，投影层不提供跨 agent 通道"
        ) from exc


def _normalize_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ValueError("每个投影条目都必须是映射对象")
    claim_key = _require_non_empty_string(item.get("claim_key"), "claim_key")
    claim_kind = _require_non_empty_string(item.get("claim_kind"), "claim_kind")
    if claim_kind not in CLAIM_KINDS:
        raise ValueError(f"claim_kind 必须是以下值之一: {CLAIM_KINDS!r}")
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content 必须是非空字符串")
    raw_ids = item.get("evidence_record_ids")
    if (
        not isinstance(raw_ids, Sequence)
        or isinstance(raw_ids, (str, bytes, bytearray))
        or not raw_ids
    ):
        raise ValueError("evidence_record_ids 必须是非空字符串列表")
    record_ids = [
        _require_non_empty_string(value, "evidence_record_ids 中的记录 ID")
        for value in raw_ids
    ]
    # Evidence order and repeated IDs do not change projection identity.
    record_ids = list(dict.fromkeys(record_ids))
    return {
        "claim_key": claim_key,
        "claim_kind": claim_kind,
        "content": content,
        "evidence_record_ids": record_ids,
    }


def _claim_id(agent_id: str, claim_key: str) -> str:
    """确定性 Claim ID：同一 agent 同一语义键恒定，跨 agent 天然不同。"""
    return "clm-" + _canonical_hash(
        [PROJECTION_SCHEMA_VERSION, "claim", agent_id, claim_key]
    )[:24]


def _source_hash(evidence_rows: Sequence[sqlite3.Row]) -> str:
    """证据集哈希：规范化的 record_id/content_sha256 对，顺序无关。"""
    pairs = sorted(
        ([row["record_id"], row["content_sha256"]] for row in evidence_rows),
        key=lambda pair: (pair[0], pair[1]),
    )
    return _canonical_hash(pairs)


def _projection_hash(
    *,
    agent_id: str,
    rule_id: str,
    rule_version: int,
    item: Mapping[str, Any],
    source_hash: str,
) -> str:
    """Fingerprint every semantic input that can change a Claim version."""
    return _canonical_hash({
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "agent_id": agent_id,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "claim_key": item["claim_key"],
        "claim_kind": item["claim_kind"],
        "content": item["content"],
        "source_hash": source_hash,
    })


def _run_id(
    rule_id: str,
    rule_version: int,
    agent_id: str,
    source_hash: str,
    projection_hashes: Sequence[str],
) -> str:
    """Idempotency key for the complete normalized projection request."""
    return "run-" + _canonical_hash({
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "agent_id": agent_id,
        "source_hash": source_hash,
        "projection_hashes": sorted(projection_hashes),
    })[:24]


def _fetch_evidence(
    conn: sqlite3.Connection, record_ids: Sequence[str]
) -> List[sqlite3.Row]:
    if not record_ids:
        raise ValueError("每条 Claim 至少需要一条证据记录")
    placeholders = ",".join("?" for _ in record_ids)
    rows = conn.execute(
        f"SELECT id, record_id, content_sha256 FROM records_v1 "
        f"WHERE record_id IN ({placeholders})",
        list(record_ids),
    ).fetchall()
    found = {row["record_id"] for row in rows}
    missing = [rid for rid in record_ids if rid not in found]
    if missing:
        raise ValueError(f"证据记录不存在: {missing}")
    return rows


def build_projection(
    items: Sequence[Mapping[str, Any]],
    *,
    agent_id: str,
    rule_id: str,
    rule_version: int = 1,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """从证据记录构建/重建 Claims，整个构建是一个事务，幂等。

    items: [{"claim_key", "claim_kind", "content", "evidence_record_ids"}]
    返回 {"run_id", "created", "superseded", "skipped", "claims"}。
    """
    agent_id = _require_agent_id(agent_id)
    rule_id = _require_non_empty_string(rule_id, "rule_id")
    if isinstance(rule_version, bool) or not isinstance(rule_version, int):
        raise ValueError("rule_version 必须是大于等于 1 的整数")
    if rule_version < 1:
        raise ValueError("rule_version 必须是大于等于 1 的整数")
    if items is None:
        raise ValueError("items 不能为空")
    try:
        raw_items = list(items)
    except TypeError as exc:
        raise ValueError("items 必须是投影条目序列") from exc
    if not raw_items:
        raise ValueError("items 不能为空")
    normalized_items = [_normalize_item(item) for item in raw_items]
    claim_keys = [item["claim_key"] for item in normalized_items]
    if len(claim_keys) != len(set(claim_keys)):
        raise ValueError("同一次构建中 claim_key 不得重复")

    migrate_records_db(db_path)
    resolved = _resolve_db_path(db_path)
    conn = _connect(resolved)
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 汇总全部证据，计算运行级 source_hash 与幂等 run_id
        per_item: List[Dict[str, Any]] = []
        all_rows: List[sqlite3.Row] = []
        for item in normalized_items:
            rows = _fetch_evidence(conn, item["evidence_record_ids"])
            source_hash = _source_hash(rows)
            per_item.append({
                "item": item,
                "rows": rows,
                "source_hash": source_hash,
                "projection_hash": _projection_hash(
                    agent_id=agent_id,
                    rule_id=rule_id,
                    rule_version=rule_version,
                    item=item,
                    source_hash=source_hash,
                ),
            })
            all_rows.extend(rows)
        unique_rows = {row["record_id"]: row for row in all_rows}
        run_source_hash = _source_hash(list(unique_rows.values()))
        run_id = _run_id(
            rule_id,
            rule_version,
            agent_id,
            run_source_hash,
            [entry["projection_hash"] for entry in per_item],
        )

        existing_run = conn.execute(
            "SELECT run_id, claim_count FROM projection_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if existing_run:
            # 幂等短路：同一构建已完整落库，直接回执，不产生任何新行
            conn.execute("COMMIT")
            return {
                "run_id": run_id, "created": 0, "superseded": 0,
                "skipped": existing_run["claim_count"], "claims": [],
                "idempotent_replay": True,
            }

        created = superseded = skipped = 0
        claim_results: List[Dict[str, Any]] = []
        for entry in per_item:
            item = entry["item"]
            rows = entry["rows"]
            src_hash = entry["source_hash"]
            projection_hash = entry["projection_hash"]
            claim_id = _claim_id(agent_id, item["claim_key"])
            active = conn.execute(
                "SELECT id, claim_version, projection_hash FROM projection_claims "
                "WHERE claim_id = ? AND agent_id = ? AND status = 'active'",
                (claim_id, agent_id),
            ).fetchone()

            if active and active["projection_hash"] == projection_hash:
                # 所有投影语义均未变：不推进版本，只补缺失链接。
                _link_evidence(conn, active["id"], rows)
                skipped += 1
                claim_results.append({
                    "claim_id": claim_id,
                    "claim_version": active["claim_version"],
                    "source_hash": src_hash,
                    "projection_hash": projection_hash,
                    "created": False,
                })
                continue

            latest = conn.execute(
                "SELECT COALESCE(MAX(claim_version), 0) AS latest_version "
                "FROM projection_claims WHERE claim_id = ? AND agent_id = ?",
                (claim_id, agent_id),
            ).fetchone()
            new_version = latest["latest_version"] + 1
            if active:
                conn.execute(
                    "UPDATE projection_claims SET status = 'superseded', "
                    "superseded_by_version = ? WHERE id = ?",
                    (new_version, active["id"]),
                )
                superseded += 1

            cur = conn.execute(
                "INSERT INTO projection_claims "
                "(claim_id, claim_version, agent_id, claim_kind, content, "
                " status, rule_id, rule_version, source_hash, projection_hash, "
                " run_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                (claim_id, new_version, agent_id, item["claim_kind"],
                 item["content"], rule_id, rule_version, src_hash,
                 projection_hash, run_id, now),
            )
            _link_evidence(conn, cur.lastrowid, rows)
            created += 1
            claim_results.append({
                "claim_id": claim_id, "claim_version": new_version,
                "source_hash": src_hash, "projection_hash": projection_hash,
                "created": True,
            })

        conn.execute(
            "INSERT INTO projection_runs "
            "(run_id, rule_id, rule_version, agent_id, source_hash, "
            " claim_count, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'completed', ?)",
            (run_id, rule_id, rule_version, agent_id, run_source_hash,
             len(per_item), now),
        )
        conn.execute("COMMIT")
        return {
            "run_id": run_id, "created": created, "superseded": superseded,
            "skipped": skipped, "claims": claim_results,
            "idempotent_replay": False,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _link_evidence(
    conn: sqlite3.Connection, claim_rowid: int, rows: Sequence[sqlite3.Row]
) -> None:
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO claim_evidence "
            "(claim_rowid, record_rowid, link_kind) VALUES (?, ?, 'supports')",
            (claim_rowid, row["id"]),
        )


def get_claim(
    claim_id: str, agent_id: str = "default", version: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """按归属读取 Claim；其他 agent 的 Claim 一律返回 None。"""
    agent_id = _require_agent_id(agent_id)
    with closing(_connect(_resolve_db_path(db_path))) as conn:
        if version is None:
            row = conn.execute(
                "SELECT * FROM projection_claims WHERE claim_id = ? AND agent_id = ? "
                "ORDER BY claim_version DESC LIMIT 1",
                (claim_id, agent_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM projection_claims WHERE claim_id = ? AND agent_id = ? "
                "AND claim_version = ?",
                (claim_id, agent_id, version),
            ).fetchone()
    return dict(row) if row else None


def list_claims(
    agent_id: str = "default", status: str = "active",
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    agent_id = _require_agent_id(agent_id)
    if status not in CLAIM_STATUSES:
        raise ValueError(f"status 必须是以下值之一: {CLAIM_STATUSES!r}")
    with closing(_connect(_resolve_db_path(db_path))) as conn:
        rows = conn.execute(
            "SELECT * FROM projection_claims WHERE agent_id = ? AND status = ? "
            "ORDER BY created_at DESC",
            (agent_id, status),
        ).fetchall()
    return [dict(r) for r in rows]


def claim_provenance(
    claim_id: str, agent_id: str = "default", version: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """返回 Claim 及其完整证据来源；跨 agent 返回 None。"""
    claim = get_claim(claim_id, agent_id, version, db_path)
    if claim is None:
        return None
    with closing(_connect(_resolve_db_path(db_path))) as conn:
        rows = conn.execute(
            "SELECT r.record_id, r.source_kind, r.source_ref, r.content, "
            "       r.content_sha256, r.verified, r.authority, ce.link_kind "
            "FROM claim_evidence ce "
            "JOIN records_v1 r ON r.id = ce.record_rowid "
            "WHERE ce.claim_rowid = ? "
            "ORDER BY r.record_id",
            (claim["id"],),
        ).fetchall()
    return {"claim": claim, "evidence": [dict(r) for r in rows]}
