"""M5-02: Claim 冲突保持与裁决审计层（来源无关、agent 隔离、不自动选边）。

原则：
- 冲突双方（或多方）Claim 均完整保留：登记与裁决都不修改、覆盖、
  删除任何 projection_claims 行，更不触碰 records_v1 证据；
- 一个冲突组容纳两条或更多 Claim（conflict_members 多对多）；
- 绝不根据置信度、authority 或来源数量自动选边：裁决只能来自显式的
  用户决定；
- 裁决是追加式审计（conflict_decisions 只插不改不删）；冲突状态在
  读取时从最新有效裁决重放派生，不保存可漂移的状态缓存；
- 状态机：open →（confirm_claim / keep_all）→ resolved；
          open →（invalidate）→ stale；
          unresolved 仅留痕，不改变状态；resolved/stale 之后仍可追加
  新裁决，最新一条决定派生状态；
- agent 归属不可绕行：conflict_id 含 agent 哈希，跨 agent 的读、
  裁决、存在性推断一律拒绝，且与"不存在"共用同一措辞；
- 幂等：重复登记同一 (agent, topic_key) 返回既有冲突并只补新成员；
  内容完全相同的裁决重复提交判为重放，不产生新行。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .projection import (
    _canonical_hash, _require_agent_id, _require_non_empty_string,
)
from .records_v1 import migrate_records_db, _connect, _resolve_db_path

CONFLICT_SCHEMA_VERSION = "echo-pact-claim-conflicts-v1"
CONFLICT_STATUSES = ("open", "resolved", "stale")
DECISIONS = ("unresolved", "confirm_claim", "keep_all", "invalidate")
MAX_RATIONALE_LENGTH = 4000
MAX_ACTOR_LENGTH = 128
_STATUS_BY_DECISION = {
    "confirm_claim": "resolved",
    "keep_all": "resolved",
    "invalidate": "stale",
}


def _conflict_id(agent_id: str, topic_key: str) -> str:
    """确定性冲突 ID：同一 agent 同一议题恒定，跨 agent 天然不同。"""
    return "cnf-" + _canonical_hash(
        [CONFLICT_SCHEMA_VERSION, "conflict", agent_id, topic_key]
    )[:24]


def _decision_id(
    conflict_id: str,
    decision: str,
    target_claim_identity: Optional[Mapping[str, Any]],
    rationale: Optional[str],
    decided_by: str,
) -> str:
    """裁决幂等键：内容完全相同的裁决重复提交判为重放。"""
    return "dec-" + _canonical_hash({
        "schema_version": CONFLICT_SCHEMA_VERSION,
        "conflict_id": conflict_id,
        "decision": decision,
        "target_claim": target_claim_identity,
        "rationale": rationale,
        "decided_by": decided_by,
    })[:24]


def _normalize_rationale(rationale: Optional[str]) -> Optional[str]:
    if rationale is None:
        return None
    if not isinstance(rationale, str):
        raise ValueError("rationale 必须是字符串或 None")
    rationale = rationale.strip()
    if not rationale:
        return None
    if len(rationale) > MAX_RATIONALE_LENGTH:
        raise ValueError(f"rationale 不得超过 {MAX_RATIONALE_LENGTH} 个字符")
    return rationale


def _resolve_claim_row(
    conn: sqlite3.Connection, agent_id: str, claim_ref: Mapping[str, Any]
) -> sqlite3.Row:
    """把 {"claim_id", "claim_version"?} 解析为归属校验过的行号。

    不传版本时取该 Claim 最新版本；跨 agent 或不存在共用同一措辞。
    """
    if not isinstance(claim_ref, Mapping):
        raise ValueError("Claim 引用必须是映射对象")
    claim_id = _require_non_empty_string(claim_ref.get("claim_id"), "claim_id")
    version = claim_ref.get("claim_version")
    if version is None:
        row = conn.execute(
            "SELECT id, claim_id, claim_version FROM projection_claims "
            "WHERE claim_id = ? AND agent_id = ? "
            "ORDER BY claim_version DESC LIMIT 1",
            (claim_id, agent_id),
        ).fetchone()
    else:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("claim_version 必须是大于等于 1 的整数")
        row = conn.execute(
            "SELECT id, claim_id, claim_version FROM projection_claims "
            "WHERE claim_id = ? AND agent_id = ? "
            "AND claim_version = ?",
            (claim_id, agent_id, version),
        ).fetchone()
    if row is None:
        raise ValueError("Claim 不存在或不属于当前 agent")
    return row


def _derive_status(conn: sqlite3.Connection, conflict_id: str) -> str:
    """Replay the latest decisive append-only event; unresolved is non-decisive."""
    row = conn.execute(
        "SELECT decision FROM conflict_decisions "
        "WHERE conflict_id = ? AND decision != 'unresolved' "
        "ORDER BY decision_seq DESC LIMIT 1",
        (conflict_id,),
    ).fetchone()
    if row is None:
        return "open"
    return _STATUS_BY_DECISION[row["decision"]]


def register_conflict(
    topic_key: str,
    claims: Sequence[Mapping[str, Any]],
    *,
    agent_id: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """登记冲突组（幂等）。claims: [{"claim_id", "claim_version"?}]，≥2 条。

    重复登记同一 (agent, topic_key)：返回既有 conflict_id，
    仅把新 Claim 补入成员（INSERT OR IGNORE），不改变状态。
    """
    agent_id = _require_agent_id(agent_id)
    topic_key = _require_non_empty_string(topic_key, "topic_key")
    if (
        not isinstance(claims, Sequence)
        or isinstance(claims, (str, bytes, bytearray))
        or len(claims) < 2
    ):
        raise ValueError("一个冲突组至少需要两条 Claim")

    migrate_records_db(db_path)
    resolved_path = _resolve_db_path(db_path)
    conn = _connect(resolved_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        claim_rows = [_resolve_claim_row(conn, agent_id, ref) for ref in claims]
        rowids = [row["id"] for row in claim_rows]
        if len(rowids) != len(set(rowids)):
            raise ValueError("同一冲突组内 Claim 不得重复")

        conflict_id = _conflict_id(agent_id, topic_key)
        existing = conn.execute(
            "SELECT conflict_id FROM claim_conflicts "
            "WHERE conflict_id = ? AND agent_id = ?",
            (conflict_id, agent_id),
        ).fetchone()
        created = False
        if existing is None:
            conn.execute(
                "INSERT INTO claim_conflicts "
                "(conflict_id, agent_id, topic_key, created_at) "
                "VALUES (?, ?, ?, ?)",
                (conflict_id, agent_id, topic_key, now),
            )
            created = True

        existing_rowids = {
            row["claim_rowid"] for row in conn.execute(
                "SELECT claim_rowid FROM conflict_members WHERE conflict_id = ?",
                (conflict_id,),
            ).fetchall()
        }
        new_rowids = [rowid for rowid in rowids if rowid not in existing_rowids]
        if new_rowids and conn.execute(
            "SELECT 1 FROM conflict_decisions WHERE conflict_id = ? LIMIT 1",
            (conflict_id,),
        ).fetchone():
            raise ValueError(
                "已有裁决审计的冲突不能追加新成员；请登记新的冲突议题"
            )

        members_added = 0
        for rowid in rowids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO conflict_members "
                "(conflict_id, agent_id, claim_rowid, role, added_at) "
                "VALUES (?, ?, ?, 'contender', ?)",
                (conflict_id, agent_id, rowid, now),
            )
            members_added += cur.rowcount
        conn.execute("COMMIT")
        return {
            "conflict_id": conflict_id,
            "created": created,
            "members_added": members_added,
            "idempotent_replay": not created and members_added == 0,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _get_conflict_row(
    conn: sqlite3.Connection, conflict_id: str, agent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM claim_conflicts WHERE conflict_id = ? AND agent_id = ?",
        (conflict_id, agent_id),
    ).fetchone()
    if row is None:
        # 不存在与跨 agent 共用同一措辞，不泄露冲突存在性
        raise ValueError("冲突不存在或不属于当前 agent")
    return row


def record_decision(
    conflict_id: str,
    decision: str,
    *,
    agent_id: str,
    target_claim: Optional[Mapping[str, Any]] = None,
    rationale: Optional[str] = None,
    decided_by: str = "user",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """追加一条用户裁决（审计只插不改），并推进冲突派生状态。

    decision:
    - unresolved：仅留痕，冲突保持原状态；
    - confirm_claim：确认某一成员 Claim（target_claim 必填且必须是成员）；
    - keep_all：均保留；
    - invalidate：冲突整体已失效。
    同一内容的裁决重复提交判为重放，不产生新行。
    """
    agent_id = _require_agent_id(agent_id)
    conflict_id = _require_non_empty_string(conflict_id, "conflict_id")
    if decision not in DECISIONS:
        raise ValueError(f"decision 必须是以下值之一: {DECISIONS!r}")
    decided_by = _require_non_empty_string(decided_by, "decided_by")
    if len(decided_by) > MAX_ACTOR_LENGTH:
        raise ValueError(f"decided_by 不得超过 {MAX_ACTOR_LENGTH} 个字符")
    rationale = _normalize_rationale(rationale)
    if decision == "confirm_claim" and target_claim is None:
        raise ValueError("confirm_claim 必须指定 target_claim")
    if decision != "confirm_claim" and target_claim is not None:
        raise ValueError("只有 confirm_claim 可以指定 target_claim")

    migrate_records_db(db_path)
    resolved_path = _resolve_db_path(db_path)
    conn = _connect(resolved_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_conflict_row(conn, conflict_id, agent_id)

        target_rowid = None
        target_identity = None
        if target_claim is not None:
            target_row = _resolve_claim_row(conn, agent_id, target_claim)
            target_rowid = target_row["id"]
            target_identity = {
                "claim_id": target_row["claim_id"],
                "claim_version": target_row["claim_version"],
            }
            member = conn.execute(
                "SELECT 1 FROM conflict_members WHERE conflict_id = ? "
                "AND agent_id = ? AND claim_rowid = ?",
                (conflict_id, agent_id, target_rowid),
            ).fetchone()
            if member is None:
                raise ValueError("confirm_claim 的目标必须是该冲突组的成员")

        decision_id = _decision_id(
            conflict_id, decision, target_identity, rationale, decided_by
        )
        existing = conn.execute(
            "SELECT decision_id FROM conflict_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if existing:
            current_status = _derive_status(conn, conflict_id)
            conn.execute("COMMIT")
            return {
                "decision_id": decision_id,
                "conflict_status": current_status,
                "idempotent_replay": True,
            }

        conn.execute(
            "INSERT INTO conflict_decisions "
            "(decision_id, conflict_id, agent_id, decision, "
            " target_claim_rowid, rationale, decided_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (decision_id, conflict_id, agent_id, decision,
             target_rowid, rationale, decided_by, now),
        )
        new_status = _derive_status(conn, conflict_id)
        conn.execute("COMMIT")
        return {
            "decision_id": decision_id,
            "conflict_status": new_status,
            "idempotent_replay": False,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def get_conflict(
    conflict_id: str,
    agent_id: str = "default",
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """返回冲突、成员与时间正序的裁决审计；跨 agent 返回 None。"""
    agent_id = _require_agent_id(agent_id)
    conflict_id = _require_non_empty_string(conflict_id, "conflict_id")
    with closing(_connect(_resolve_db_path(db_path))) as conn:
        from .identity import EvidenceNotVisible, require_all_claim_evidence_visible

        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            "SELECT * FROM claim_conflicts WHERE conflict_id = ? AND agent_id = ?",
            (conflict_id, agent_id),
        ).fetchone()
        if row is None:
            return None
        conflict = dict(row)
        conflict["status"] = _derive_status(conn, conflict_id)
        members = conn.execute(
            "SELECT cm.member_seq, cm.claim_rowid, cm.role, cm.added_at, "
            "       pc.claim_id, pc.claim_version, pc.claim_kind, pc.content, "
            "       pc.status AS claim_status "
            "FROM conflict_members cm "
            "JOIN projection_claims pc ON pc.id = cm.claim_rowid "
            "WHERE cm.conflict_id = ? ORDER BY cm.member_seq",
            (conflict_id,),
        ).fetchall()
        member_views = []
        for member in members:
            try:
                require_all_claim_evidence_visible(
                    conn, agent_id, member["claim_rowid"]
                )
            except EvidenceNotVisible:
                return {
                    "conflict": {
                        "conflict_id": conflict_id,
                        "status": "restricted",
                    },
                    "members": [],
                    "decisions": [],
                }
            member_view = dict(member)
            evidence = conn.execute(
                "SELECT r.record_id, r.source_kind, r.source_ref, r.verified, "
                "       r.authority, r.conflict_group_id, r.source_cutoff_at "
                "FROM claim_evidence ce "
                "JOIN records_v1 r ON r.id = ce.record_rowid "
                "WHERE ce.claim_rowid = ? ORDER BY r.record_id",
                (member["claim_rowid"],),
            ).fetchall()
            member_view["evidence"] = [dict(row) for row in evidence]
            member_views.append(member_view)
        decisions = conn.execute(
            "SELECT * FROM conflict_decisions WHERE conflict_id = ? "
            "ORDER BY decision_seq",
            (conflict_id,),
        ).fetchall()
    return {
        "conflict": conflict,
        "members": member_views,
        "decisions": [dict(r) for r in decisions],
    }


def list_conflicts(
    agent_id: str = "default",
    status: Optional[str] = None,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    agent_id = _require_agent_id(agent_id)
    if status is not None and status not in CONFLICT_STATUSES:
        raise ValueError(f"status 必须是以下值之一: {CONFLICT_STATUSES!r}")
    with closing(_connect(_resolve_db_path(db_path))) as conn:
        from .identity import EvidenceNotVisible, require_all_claim_evidence_visible

        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT * FROM claim_conflicts WHERE agent_id = ? "
            "ORDER BY created_at DESC",
            (agent_id,),
        ).fetchall()
        results = []
        for row in rows:
            member_ids = conn.execute(
                "SELECT claim_rowid FROM conflict_members "
                "WHERE conflict_id = ? AND agent_id = ?",
                (row["conflict_id"], agent_id),
            ).fetchall()
            try:
                for member in member_ids:
                    require_all_claim_evidence_visible(
                        conn, agent_id, member["claim_rowid"]
                    )
            except EvidenceNotVisible:
                continue
            item = dict(row)
            item["status"] = _derive_status(conn, item["conflict_id"])
            if status is None or item["status"] == status:
                results.append(item)
    return results
