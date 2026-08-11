"""M5-02 冲突保持与裁决审计测试（纯合成数据）。

覆盖工单验收面：
- 冲突双方 Claim 均完整保留，裁决不修改任何 Claim 或证据；
- 冲突组容纳两条及以上 Claim；
- 不按置信度/authority/来源数量自动选边（只有显式裁决改变状态）；
- 裁决追加式审计：未决/确认某一 Claim/均保留/已失效；
- 跨 agent 不可读取、裁决或推断冲突存在性；
- 重复登记与重复裁决的幂等语义；
- 事务化迁移与回滚。
"""
import sqlite3
from pathlib import Path

import pytest

import backend.utils.db as db_module
import backend.memory.records_v1 as records_module
from backend.memory.projection import build_projection, get_claim
from backend.memory.claim_conflicts import (
    register_conflict, record_decision, get_conflict, list_conflicts,
)
from backend.memory.records_v1 import import_record_package, migrate_records_db

FIXTURE = Path(__file__).parent / "fixtures" / "echo_pact_records_v1.json"
REC_MAIN_1 = "synthetic-lighthouse-main-001"
REC_MAIN_2 = "synthetic-lighthouse-main-002"
REC_ALT = "synthetic-lighthouse-alt-001"
REC_PATCH = "synthetic-lighthouse-patch-001"
TOPIC = "lighthouse:identity"


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "conflict.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    import_record_package(str(FIXTURE), db_path=str(db_path))
    return str(db_path)


def _build_claims(setup_db, agent_id="agent-a"):
    items = [
        {"claim_key": "lh:main", "claim_kind": "fact",
         "content": "灯塔计划的离线代号是 ORCHID-731",
         "evidence_record_ids": [REC_MAIN_1, REC_MAIN_2]},
        {"claim_key": "lh:alt", "claim_kind": "fact",
         "content": "灯塔计划的备用色是琥珀色",
         "evidence_record_ids": [REC_ALT]},
        {"claim_key": "lh:patch", "claim_kind": "note",
         "content": "ORCHID-731 的演示标签是 NOVA",
         "evidence_record_ids": [REC_PATCH]},
    ]
    result = build_projection(items, agent_id=agent_id, rule_id="r1", db_path=setup_db)
    return [c["claim_id"] for c in result["claims"]]


def _refs(*claim_ids):
    return [{"claim_id": cid} for cid in claim_ids]


def _table_counts(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("claim_conflicts", "conflict_members", "conflict_decisions")
        }


def _register(setup_db, agent_id="agent-a", topic=TOPIC, n=2):
    ids = _build_claims(setup_db, agent_id)
    result = register_conflict(topic, _refs(*ids[:n]), agent_id=agent_id, db_path=setup_db)
    return result["conflict_id"], ids


# ---------- 登记 ----------

def test_register_creates_open_conflict_with_members(setup_db):
    conflict_id, ids = _register(setup_db)
    view = get_conflict(conflict_id, agent_id="agent-a", db_path=setup_db)
    assert view["conflict"]["status"] == "open"
    assert view["conflict"]["topic_key"] == TOPIC
    member_claims = {m["claim_id"] for m in view["members"]}
    assert member_claims == set(ids[:2])
    assert view["decisions"] == []


def test_conflict_view_exposes_sources_without_auto_selecting(setup_db):
    ids = _build_claims(setup_db)
    conflict_id = register_conflict(
        "lighthouse:official-vs-patch", _refs(ids[0], ids[2]),
        agent_id="agent-a", db_path=setup_db,
    )["conflict_id"]
    view = get_conflict(conflict_id, agent_id="agent-a", db_path=setup_db)
    members = {member["claim_id"]: member for member in view["members"]}

    assert members[ids[0]]["content"] == "灯塔计划的离线代号是 ORCHID-731"
    assert {evidence["authority"] for evidence in members[ids[0]]["evidence"]} == {
        "official", "user-confirmed",
    }
    patch_evidence = members[ids[2]]["evidence"]
    assert len(patch_evidence) == 1
    assert patch_evidence[0]["source_kind"] == "recent_patch"
    assert patch_evidence[0]["verified"] == 0
    assert patch_evidence[0]["authority"] == "unverified-patch"
    assert patch_evidence[0]["source_ref"]
    # 展示来源强弱不等于自动选边；只有显式裁决会改变状态。
    assert view["conflict"]["status"] == "open"
    assert view["decisions"] == []


def test_register_is_idempotent(setup_db):
    ids = _build_claims(setup_db)
    first = register_conflict(TOPIC, _refs(*ids[:2]), agent_id="agent-a", db_path=setup_db)
    second = register_conflict(TOPIC, _refs(*ids[:2]), agent_id="agent-a", db_path=setup_db)
    assert second["conflict_id"] == first["conflict_id"]
    assert second["idempotent_replay"] is True
    assert _table_counts(setup_db) == {
        "claim_conflicts": 1, "conflict_members": 2, "conflict_decisions": 0,
    }


def test_reregister_extends_group_beyond_two(setup_db):
    ids = _build_claims(setup_db)
    register_conflict(TOPIC, _refs(*ids[:2]), agent_id="agent-a", db_path=setup_db)
    third = register_conflict(TOPIC, _refs(*ids), agent_id="agent-a", db_path=setup_db)
    assert third["created"] is False and third["members_added"] == 1
    view = get_conflict(third["conflict_id"], agent_id="agent-a", db_path=setup_db)
    assert len(view["members"]) == 3


def test_register_requires_two_distinct_claims(setup_db):
    ids = _build_claims(setup_db)
    with pytest.raises(ValueError):
        register_conflict(TOPIC, _refs(ids[0]), agent_id="agent-a", db_path=setup_db)
    with pytest.raises(ValueError):
        register_conflict(TOPIC, _refs(ids[0], ids[0]), agent_id="agent-a", db_path=setup_db)
    assert _table_counts(setup_db)["claim_conflicts"] == 0


def test_register_rejects_cross_agent_claim_non_revealing(setup_db):
    own = _build_claims(setup_db, "agent-a")
    other = _build_claims(setup_db, "agent-b")
    with pytest.raises(ValueError) as exc_info:
        register_conflict(TOPIC, _refs(own[0], other[1]), agent_id="agent-a", db_path=setup_db)
    assert "agent-b" not in str(exc_info.value)


# ---------- 裁决状态机 ----------

def test_unresolved_decision_keeps_open(setup_db):
    conflict_id, ids = _register(setup_db)
    result = record_decision(conflict_id, "unresolved", agent_id="agent-a",
                             rationale="再看看", db_path=setup_db)
    assert result["conflict_status"] == "open"
    view = get_conflict(conflict_id, agent_id="agent-a", db_path=setup_db)
    assert len(view["decisions"]) == 1


def test_confirm_claim_resolves_and_preserves_both_claims(setup_db):
    conflict_id, ids = _register(setup_db)
    result = record_decision(
        conflict_id, "confirm_claim", agent_id="agent-a",
        target_claim={"claim_id": ids[0]}, rationale="馆长说是这个",
        db_path=setup_db,
    )
    assert result["conflict_status"] == "resolved"
    view = get_conflict(conflict_id, agent_id="agent-a", db_path=setup_db)
    decision = view["decisions"][-1]
    assert decision["decision"] == "confirm_claim"
    assert decision["target_claim_rowid"] is not None
    # 两条 Claim 均完整保留：内容、状态原样，不被覆盖
    for cid in ids[:2]:
        claim = get_claim(cid, agent_id="agent-a", db_path=setup_db)
        assert claim["status"] == "active"


def test_keep_all_resolves(setup_db):
    conflict_id, _ = _register(setup_db)
    result = record_decision(conflict_id, "keep_all", agent_id="agent-a", db_path=setup_db)
    assert result["conflict_status"] == "resolved"


def test_invalidate_marks_stale(setup_db):
    conflict_id, _ = _register(setup_db)
    result = record_decision(conflict_id, "invalidate", agent_id="agent-a",
                             rationale="议题已过时", db_path=setup_db)
    assert result["conflict_status"] == "stale"


def test_decisions_are_append_only_audit(setup_db):
    conflict_id, ids = _register(setup_db)
    record_decision(conflict_id, "unresolved", agent_id="agent-a", db_path=setup_db)
    record_decision(conflict_id, "confirm_claim", agent_id="agent-a",
                    target_claim={"claim_id": ids[0]}, db_path=setup_db)
    record_decision(conflict_id, "keep_all", agent_id="agent-a",
                    rationale="改主意了，都留", db_path=setup_db)
    view = get_conflict(conflict_id, agent_id="agent-a", db_path=setup_db)
    assert [d["decision"] for d in view["decisions"]] == [
        "unresolved", "confirm_claim", "keep_all",
    ]
    assert view["conflict"]["status"] == "resolved"
    assert _table_counts(setup_db)["conflict_decisions"] == 3


def test_status_is_replayed_from_append_only_decisions(setup_db):
    conflict_id, _ = _register(setup_db)
    record_decision(
        conflict_id, "keep_all", agent_id="agent-a", db_path=setup_db
    )
    record_decision(
        conflict_id, "unresolved", agent_id="agent-a",
        rationale="后续仍需观察", db_path=setup_db,
    )
    view = get_conflict(conflict_id, agent_id="agent-a", db_path=setup_db)
    assert view["conflict"]["status"] == "resolved"
    assert list_conflicts(
        agent_id="agent-a", status="resolved", db_path=setup_db
    )[0]["conflict_id"] == conflict_id
    with sqlite3.connect(setup_db) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(claim_conflicts)")
        }
    assert "status" not in columns  # 状态不是可漂移的存储字段


def test_decision_idempotent_replay(setup_db):
    conflict_id, ids = _register(setup_db)
    kwargs = dict(agent_id="agent-a", target_claim={"claim_id": ids[0]},
                  rationale="同上", decided_by="user", db_path=setup_db)
    first = record_decision(conflict_id, "confirm_claim", **kwargs)
    second = record_decision(conflict_id, "confirm_claim", **kwargs)
    assert second["decision_id"] == first["decision_id"]
    assert second["idempotent_replay"] is True
    assert _table_counts(setup_db)["conflict_decisions"] == 1


def test_decision_id_uses_stable_claim_identity(setup_db, tmp_path):
    conflict_id, ids = _register(setup_db)
    first = record_decision(
        conflict_id, "confirm_claim", agent_id="agent-a",
        target_claim={"claim_id": ids[0]}, rationale="同一裁决",
        db_path=setup_db,
    )

    second_db = tmp_path / "shifted-rowids.db"
    import_record_package(str(FIXTURE), db_path=str(second_db))
    build_projection(
        [{"claim_key": "dummy", "claim_kind": "note", "content": "占位",
          "evidence_record_ids": [REC_PATCH]}],
        agent_id="agent-a", rule_id="dummy-rule", db_path=str(second_db),
    )
    second_ids = _build_claims(str(second_db), "agent-a")
    second_conflict = register_conflict(
        TOPIC, _refs(*second_ids[:2]), agent_id="agent-a", db_path=str(second_db)
    )["conflict_id"]
    second = record_decision(
        second_conflict, "confirm_claim", agent_id="agent-a",
        target_claim={"claim_id": second_ids[0]}, rationale="同一裁决",
        db_path=str(second_db),
    )
    first_view = get_conflict(conflict_id, "agent-a", setup_db)
    second_view = get_conflict(second_conflict, "agent-a", str(second_db))
    assert first_view["decisions"][0]["target_claim_rowid"] != (
        second_view["decisions"][0]["target_claim_rowid"]
    )
    assert first["decision_id"] == second["decision_id"]


def test_confirm_requires_member_target(setup_db):
    conflict_id, ids = _register(setup_db, n=2)
    with pytest.raises(ValueError) as exc_info:
        record_decision(conflict_id, "confirm_claim", agent_id="agent-a",
                        target_claim={"claim_id": ids[2]}, db_path=setup_db)
    assert "成员" in str(exc_info.value)
    # 回滚：无裁决落库，状态仍 open
    view = get_conflict(conflict_id, agent_id="agent-a", db_path=setup_db)
    assert view["decisions"] == [] and view["conflict"]["status"] == "open"


def test_decided_conflict_cannot_silently_gain_new_members(setup_db):
    ids = _build_claims(setup_db)
    conflict_id = register_conflict(
        TOPIC, _refs(*ids[:2]), agent_id="agent-a", db_path=setup_db
    )["conflict_id"]
    record_decision(
        conflict_id, "keep_all", agent_id="agent-a", db_path=setup_db
    )
    with pytest.raises(ValueError, match="不能追加新成员"):
        register_conflict(
            TOPIC, _refs(*ids), agent_id="agent-a", db_path=setup_db
        )
    view = get_conflict(conflict_id, agent_id="agent-a", db_path=setup_db)
    assert len(view["members"]) == 2
    assert view["conflict"]["status"] == "resolved"

    with sqlite3.connect(setup_db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        extra_rowid = conn.execute(
            "SELECT id FROM projection_claims WHERE claim_id = ?", (ids[2],)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="cannot accept"):
            conn.execute(
                "INSERT INTO conflict_members "
                "(conflict_id, agent_id, claim_rowid, role, added_at) "
                "VALUES (?, 'agent-a', ?, 'contender', 'synthetic')",
                (conflict_id, extra_rowid),
            )
        conn.rollback()
    assert len(
        get_conflict(conflict_id, agent_id="agent-a", db_path=setup_db)["members"]
    ) == 2


def test_decision_target_rules(setup_db):
    conflict_id, ids = _register(setup_db)
    with pytest.raises(ValueError):
        record_decision(conflict_id, "confirm_claim", agent_id="agent-a", db_path=setup_db)
    with pytest.raises(ValueError):
        record_decision(conflict_id, "keep_all", agent_id="agent-a",
                        target_claim={"claim_id": ids[0]}, db_path=setup_db)
    with pytest.raises(ValueError):
        record_decision(conflict_id, "auto_pick", agent_id="agent-a", db_path=setup_db)


def test_invalid_conflict_payloads_fail_closed(setup_db):
    ids = _build_claims(setup_db)
    with pytest.raises(ValueError):
        register_conflict(TOPIC, "not-a-list", agent_id="agent-a", db_path=setup_db)
    with pytest.raises(ValueError):
        register_conflict(
            TOPIC, ["bad-ref", {"claim_id": ids[0]}],
            agent_id="agent-a", db_path=setup_db,
        )
    conflict_id = register_conflict(
        TOPIC, _refs(*ids[:2]), agent_id="agent-a", db_path=setup_db
    )["conflict_id"]
    with pytest.raises(ValueError):
        record_decision(
            conflict_id, "confirm_claim", agent_id="agent-a",
            target_claim="bad-ref", db_path=setup_db,
        )
    with pytest.raises(ValueError, match="rationale"):
        record_decision(
            conflict_id, "keep_all", agent_id="agent-a",
            rationale=123, db_path=setup_db,
        )
    with pytest.raises(ValueError, match="rationale"):
        record_decision(
            conflict_id, "keep_all", agent_id="agent-a",
            rationale="x" * 4001, db_path=setup_db,
        )
    with pytest.raises(ValueError, match="decided_by"):
        record_decision(
            conflict_id, "keep_all", agent_id="agent-a",
            decided_by="x" * 129, db_path=setup_db,
        )


# ---------- agent 隔离 ----------

def test_cross_agent_cannot_read_decide_or_infer(setup_db):
    conflict_id, ids = _register(setup_db, agent_id="agent-a")
    assert get_conflict(conflict_id, agent_id="agent-b", db_path=setup_db) is None
    assert list_conflicts(agent_id="agent-b", db_path=setup_db) == []
    with pytest.raises(ValueError) as cross:
        record_decision(conflict_id, "keep_all", agent_id="agent-b", db_path=setup_db)
    with pytest.raises(ValueError) as missing:
        record_decision("cnf-doesnotexist", "keep_all", agent_id="agent-b", db_path=setup_db)
    # 跨 agent 与不存在共用同一措辞：存在性不可推断
    assert str(cross.value) == str(missing.value)
    assert "agent-a" not in str(cross.value)


def test_same_topic_different_agents_are_distinct(setup_db):
    ids_a = _build_claims(setup_db, "agent-a")
    ids_b = _build_claims(setup_db, "agent-b")
    ra = register_conflict(TOPIC, _refs(*ids_a[:2]), agent_id="agent-a", db_path=setup_db)
    rb = register_conflict(TOPIC, _refs(*ids_b[:2]), agent_id="agent-b", db_path=setup_db)
    assert ra["conflict_id"] != rb["conflict_id"]
    assert len(list_conflicts(agent_id="agent-a", db_path=setup_db)) == 1
    assert len(list_conflicts(agent_id="agent-b", db_path=setup_db)) == 1


@pytest.mark.parametrize("bad_agent", [None, "", "   "])
def test_empty_agent_rejected_everywhere(setup_db, bad_agent):
    conflict_id, ids = _register(setup_db)
    with pytest.raises(ValueError):
        register_conflict("t2", _refs(*ids[:2]), agent_id=bad_agent, db_path=setup_db)
    with pytest.raises(ValueError):
        record_decision(conflict_id, "keep_all", agent_id=bad_agent, db_path=setup_db)
    with pytest.raises(ValueError):
        get_conflict(conflict_id, agent_id=bad_agent, db_path=setup_db)
    with pytest.raises(ValueError):
        list_conflicts(agent_id=bad_agent, db_path=setup_db)


# ---------- 迁移与证据不可变 ----------

def test_migration_v4_forward_only_repeatable(setup_db):
    result = migrate_records_db(setup_db)
    assert result["applied"] == []
    assert result["current_version"] == 4


def test_conflicts_never_touch_claims_or_evidence(setup_db):
    def snapshot(db_path):
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            claims = [dict(r) for r in conn.execute(
                "SELECT claim_id, claim_version, content, status, source_hash "
                "FROM projection_claims ORDER BY id")]
            records = [dict(r) for r in conn.execute(
                "SELECT record_id, content_sha256 FROM records_v1 ORDER BY id")]
        return claims, records

    ids = _build_claims(setup_db)
    before = snapshot(setup_db)
    conflict_id = register_conflict(
        TOPIC, _refs(*ids[:2]), agent_id="agent-a", db_path=setup_db
    )["conflict_id"]
    record_decision(conflict_id, "confirm_claim", agent_id="agent-a",
                    target_claim={"claim_id": ids[0]}, db_path=setup_db)
    record_decision(conflict_id, "invalidate", agent_id="agent-a", db_path=setup_db)
    assert snapshot(setup_db) == before


def test_conflict_audit_tables_reject_update_and_delete(setup_db):
    conflict_id, ids = _register(setup_db)
    record_decision(
        conflict_id, "confirm_claim", agent_id="agent-a",
        target_claim={"claim_id": ids[0]}, db_path=setup_db,
    )
    operations = [
        ("UPDATE claim_conflicts SET topic_key='tampered' WHERE conflict_id=?",
         (conflict_id,)),
        ("DELETE FROM claim_conflicts WHERE conflict_id=?", (conflict_id,)),
        ("UPDATE conflict_members SET role='contender' WHERE conflict_id=?",
         (conflict_id,)),
        ("DELETE FROM conflict_members WHERE conflict_id=?", (conflict_id,)),
        ("UPDATE conflict_decisions SET rationale='tampered' WHERE conflict_id=?",
         (conflict_id,)),
        ("DELETE FROM conflict_decisions WHERE conflict_id=?", (conflict_id,)),
    ]
    with sqlite3.connect(setup_db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for sql, params in operations:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql, params)
            conn.rollback()
    assert _table_counts(setup_db) == {
        "claim_conflicts": 1, "conflict_members": 2, "conflict_decisions": 1,
    }


def test_database_rejects_cross_agent_and_nonmember_decisions(setup_db):
    conflict_id, ids = _register(setup_db)
    with sqlite3.connect(setup_db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        claim_rowid = conn.execute(
            "SELECT id FROM projection_claims WHERE claim_id = ?", (ids[0],)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO conflict_members "
                "(conflict_id, agent_id, claim_rowid, role, added_at) "
                "VALUES (?, 'agent-b', ?, 'contender', 'synthetic')",
                (conflict_id, claim_rowid),
            )
        conn.rollback()
        nonmember_rowid = conn.execute(
            "SELECT id FROM projection_claims WHERE claim_id = ?", (ids[2],)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO conflict_decisions "
                "(decision_id, conflict_id, agent_id, decision, "
                " target_claim_rowid, rationale, decided_by, created_at) "
                "VALUES ('dec-invalid', ?, 'agent-a', 'confirm_claim', ?, "
                " NULL, 'user', 'synthetic')",
                (conflict_id, nonmember_rowid),
            )
        conn.rollback()


def test_migration_v4_failure_rolls_back_only_v4(tmp_path, monkeypatch):
    db_path = tmp_path / "conflict-migration-failure.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for statements in (
            records_module.MIGRATION_1_STATEMENTS,
            records_module.MIGRATION_2_STATEMENTS,
            records_module.MIGRATION_3_STATEMENTS,
        ):
            for statement in statements:
                conn.execute(statement)
        conn.executemany(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            [(1, "v1", "synthetic"), (2, "v2", "synthetic"),
             (3, "v3", "synthetic")],
        )
    monkeypatch.setattr(
        records_module,
        "MIGRATION_4_STATEMENTS",
        records_module.MIGRATION_4_STATEMENTS + ("THIS IS NOT VALID SQL",),
    )
    with pytest.raises(sqlite3.OperationalError):
        migrate_records_db(str(db_path))
    with sqlite3.connect(db_path) as conn:
        versions = [
            row[0] for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        conflict_tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name LIKE 'conflict_%'"
        ).fetchone()[0]
        projection_tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name LIKE 'projection_%'"
        ).fetchone()[0]
        assert versions == [1, 2, 3]
        assert conflict_tables == 0
        assert projection_tables == 2
