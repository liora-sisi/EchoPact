"""M5-03: 召回接合层（召回认领 / 裁决回流 / 投影新鲜度）。

证据召回核心保持不变。本模块只给召回结果附加当前 agent 的 active
Claim、冲突裁决呈现和投影新鲜度，不改排序、置信度或任何持久数据。
"""

from __future__ import annotations

from contextlib import closing
from typing import Any, Dict, List, Optional

from .claim_conflicts import _derive_status
from .identity import (
    EvidenceNotVisible,
    _can_read_derived,
    current_visibility,
    require_all_claim_evidence_visible,
)
from .projection import _require_agent_id, _source_hash
from .records_v1 import (
    _connect,
    _connect_readonly,
    _resolve_db_path,
    migrate_records_db,
    recall_records,
)

RECALL_PROJECTION_SCHEMA_VERSION = "echo-pact-recall-projection-v1"

CONFLICT_PRESENTATION = {
    "open": "存在未决冲突：组内 Claim 均完整保留，等待用户裁决",
    "resolved": "用户已裁决：按有效裁决标注，组内 Claim 均保留可溯",
    "stale": "用户已裁决此冲突失效：标注留痕，Claim 不删不改",
}

ADJUDICATION_NOTE = (
    "裁决仅影响呈现标注：不隐藏记录、不改权重、不自动选边；"
    "证据层与投影层数据一行不动。"
)


def _decision_view(
    conn,
    conflict_id: str,
    *,
    decisive_only: bool,
) -> Optional[Dict[str, Any]]:
    """Return one stable decision view without leaking SQLite row identities."""
    decisive_filter = "AND cd.decision != 'unresolved'" if decisive_only else ""
    row = conn.execute(
        "SELECT cd.decision, cd.rationale, cd.decided_by, cd.created_at, "
        "       pc.claim_id AS target_claim_id, "
        "       pc.claim_version AS target_claim_version "
        "FROM conflict_decisions cd "
        "LEFT JOIN projection_claims pc ON pc.id = cd.target_claim_rowid "
        "WHERE cd.conflict_id = ? "
        f"{decisive_filter} "
        "ORDER BY cd.decision_seq DESC LIMIT 1",
        (conflict_id,),
    ).fetchone()
    if row is None:
        return None
    item = {
        "decision": row["decision"],
        "rationale": row["rationale"],
        "decided_by": row["decided_by"],
        "created_at": row["created_at"],
    }
    if row["target_claim_id"] is not None:
        item["target_claim"] = {
            "claim_id": row["target_claim_id"],
            "claim_version": row["target_claim_version"],
        }
    return item


def _claim_freshness(conn, claim_rowid: int, stored_source_hash: str) -> Dict[str, Any]:
    """Recompute a Claim evidence hash; never silently serve a drifted projection."""
    rows = conn.execute(
        "SELECT r.record_id, r.content_sha256 FROM claim_evidence ce "
        "JOIN records_v1 r ON r.id = ce.record_rowid "
        "WHERE ce.claim_rowid = ? ORDER BY r.record_id",
        (claim_rowid,),
    ).fetchall()
    linked = conn.execute(
        "SELECT COUNT(*) AS n FROM claim_evidence WHERE claim_rowid = ?",
        (claim_rowid,),
    ).fetchone()["n"]
    if len(rows) != linked:
        return {"freshness": "stale", "stale_reason": "evidence_orphaned"}
    if not rows:
        return {"freshness": "stale", "stale_reason": "evidence_missing"}
    if _source_hash(rows) != stored_source_hash:
        return {"freshness": "stale", "stale_reason": "source_hash_mismatch"}
    return {"freshness": "fresh", "stale_reason": None}


def _group_members_visible(conn, agent_id: str, conflict_id: str) -> bool:
    """冲突组内任一成员的证据不可见 → 整组脱敏。"""
    members = conn.execute(
        "SELECT claim_rowid FROM conflict_members WHERE conflict_id = ? "
        "AND agent_id = ?",
        (conflict_id, agent_id),
    ).fetchall()
    for member in members:
        evidence = conn.execute(
            "SELECT record_rowid FROM claim_evidence WHERE claim_rowid = ?",
            (member["claim_rowid"],),
        ).fetchall()
        for row in evidence:
            if not _can_read_derived(
                agent_id, current_visibility(conn, row["record_rowid"])
            ):
                return False
    return True


def _conflict_annotations(
    conn, agent_id: str, claim_rowid: int
) -> List[Dict[str, Any]]:
    conflicts = conn.execute(
        "SELECT cc.conflict_id, cc.topic_key FROM conflict_members cm "
        "JOIN claim_conflicts cc ON cc.conflict_id = cm.conflict_id "
        "AND cc.agent_id = cm.agent_id "
        "WHERE cm.agent_id = ? AND cm.claim_rowid = ? "
        "ORDER BY cm.member_seq",
        (agent_id, claim_rowid),
    ).fetchall()
    annotations: List[Dict[str, Any]] = []
    for conflict in conflicts:
        conflict_id = conflict["conflict_id"]
        if not _group_members_visible(conn, agent_id, conflict_id):
            # 组成员存在不可见证据：topic/rationale/target/decided_by 全抑制
            annotations.append({"conflict_id": conflict_id,
                                "status": "restricted"})
            continue
        status = _derive_status(conn, conflict_id)
        annotations.append(
            {
                "conflict_id": conflict_id,
                "topic_key": conflict["topic_key"],
                "status": status,
                # State-driving event: unresolved audit notes never mask it.
                "latest_decision": _decision_view(
                    conn, conflict_id, decisive_only=True
                ),
                # Complete chronology still exposes the newest audit event.
                "latest_audit_event": _decision_view(
                    conn, conflict_id, decisive_only=False
                ),
                "presentation": CONFLICT_PRESENTATION[status],
            }
        )
    return annotations


def recall_with_projection(
    query: str,
    *,
    agent_id: str,
    limit: int = 5,
    as_of: Optional[str] = None,
    db_path: Optional[str] = None,
    read_only: bool = False,
) -> Dict[str, Any]:
    """Attach current-agent projection and adjudication views to V1 recall."""
    agent_id = _require_agent_id(agent_id)
    # Network/API callers preserve the existing forward-only migration
    # contract.  External read-only adapters instead fail closed on schema
    # drift and never acquire a write-capable connection.
    if not read_only:
        migrate_records_db(db_path)
    # M5-04：证据召回必须先过可见范围，不可见记录连 memories 列表都进不来；
    # claim 级 evidence_not_visible 只负责跨页证据的脱敏占位。
    base = recall_records(
        query,
        limit=limit,
        as_of=as_of,
        db_path=db_path,
        agent_id=agent_id,
        read_only=read_only,
    )

    memories = base.get("memories", [])
    connect = _connect_readonly if read_only else _connect
    with closing(connect(_resolve_db_path(db_path))) as conn:
        # Enforce the join layer's read-only promise at the SQLite connection.
        conn.execute("PRAGMA query_only = ON")
        for memory in memories:
            claim_rows = conn.execute(
                "SELECT pc.id, pc.claim_id, pc.claim_version, pc.claim_kind, "
                "       pc.content, pc.source_hash, pc.rule_id, pc.rule_version "
                "FROM claim_evidence ce "
                "JOIN records_v1 r ON r.id = ce.record_rowid "
                "JOIN projection_claims pc ON pc.id = ce.claim_rowid "
                "WHERE r.record_id = ? AND pc.agent_id = ? "
                "AND pc.status = 'active' "
                "ORDER BY pc.claim_id, pc.claim_version",
                (memory["record_id"], agent_id),
            ).fetchall()
            claims: List[Dict[str, Any]] = []
            for row in claim_rows:
                try:
                    require_all_claim_evidence_visible(conn, agent_id, row["id"])
                except EvidenceNotVisible:
                    # 修 7：restricted 全面脱敏，绝不返回 freshness/stale_reason
                    claims.append(
                        {
                            "claim_id": row["claim_id"],
                            "claim_version": row["claim_version"],
                            "restricted": True,
                            "restricted_reason": "evidence_not_visible",
                        }
                    )
                    continue
                freshness = _claim_freshness(conn, row["id"], row["source_hash"])
                claims.append(
                    {
                        "claim_id": row["claim_id"],
                        "claim_version": row["claim_version"],
                        "claim_kind": row["claim_kind"],
                        "content": row["content"],
                        "rule_id": row["rule_id"],
                        "rule_version": row["rule_version"],
                        "freshness": freshness["freshness"],
                        "stale_reason": freshness["stale_reason"],
                        "conflicts": _conflict_annotations(conn, agent_id, row["id"]),
                    }
                )
            memory["claims"] = claims
            if claims and all(c.get("restricted") for c in claims):
                memory["projection_status"] = "restricted"
            else:
                memory["projection_status"] = (
                    "projected" if claims else "unprojected"
                )

    base["schema_version"] = RECALL_PROJECTION_SCHEMA_VERSION
    base["evidence_schema_version"] = "echo-pact-recall-v1"
    base["agent_id"] = agent_id
    base["adjudication_note"] = ADJUDICATION_NOTE
    return base
