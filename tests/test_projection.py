"""M5-01 投影层地基测试（纯合成数据）。

覆盖工单验收面：
- 数据模型与版本化向前迁移；
- Claim ↔ 证据多对多可追溯（一对多、多对一、多来源保真）；
- agent_id 归属明确且不可绕行；
- 投影版本、生成规则、来源哈希留痕；
- 幂等创建/重建；
- 证据层不覆盖、不合并、不删除；
- 事务回滚。
"""
import sqlite3
from pathlib import Path

import pytest

import backend.utils.db as db_module
import backend.memory.records_v1 as records_module
from backend.memory.identity import register_agent, set_scope
from backend.memory.projection import (
    build_projection, get_claim, list_claims, claim_provenance,
)
from backend.memory.records_v1 import import_record_package, migrate_records_db

FIXTURE = Path(__file__).parent / "fixtures" / "echo_pact_records_v1.json"

REC_MAIN_1 = "synthetic-lighthouse-main-001"
REC_MAIN_2 = "synthetic-lighthouse-main-002"
REC_ALT = "synthetic-lighthouse-alt-001"
REC_PATCH = "synthetic-lighthouse-patch-001"


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "proj.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    # M5-04：投影构建有可见性闸门。fixture 证据归属 agent-a 并设为 shared，
    # 保持"多 agent 共享证据、各自命名空间建 Claim"的原测试语义
    register_agent("agent-a", "测试 Agent A", actor="test", db_path=str(db_path))
    register_agent("agent-b", "测试 Agent B", actor="test", db_path=str(db_path))
    import_record_package(
        str(FIXTURE), db_path=str(db_path), owner_agent_id="agent-a"
    )
    for record_id in (REC_MAIN_1, REC_MAIN_2, REC_ALT, REC_PATCH):
        set_scope(record_id, "shared", actor="test", db_path=str(db_path))
    return str(db_path)


def _items(**overrides):
    base = {
        "claim_key": "lighthouse:codename",
        "claim_kind": "fact",
        "content": "灯塔计划的离线代号是 ORCHID-731",
        "evidence_record_ids": [REC_MAIN_1, REC_MAIN_2],
    }
    base.update(overrides)
    return [base]


def _evidence_hashes(setup_db):
    with sqlite3.connect(setup_db) as conn:
        conn.row_factory = sqlite3.Row
        return {
            r["record_id"]: r["content_sha256"]
            for r in conn.execute("SELECT record_id, content_sha256 FROM records_v1")
        }


# ---------- 构建与留痕 ----------

def test_build_creates_claim_with_full_trace(setup_db):
    result = build_projection(
        _items(), agent_id="agent-a", rule_id="synthetic-rule-v1", rule_version=1,
        db_path=setup_db,
    )
    assert result["created"] == 1 and result["run_id"].startswith("run-")
    claim = get_claim(result["claims"][0]["claim_id"], agent_id="agent-a", db_path=setup_db)
    assert claim["rule_id"] == "synthetic-rule-v1"
    assert claim["rule_version"] == 1
    assert claim["claim_version"] == 1
    assert claim["status"] == "active"
    assert claim["source_hash"]
    assert claim["run_id"] == result["run_id"]


def test_migration_v3_forward_only_repeatable(setup_db):
    first = migrate_records_db(setup_db)
    assert first["applied"] == []  # fixture 导入时已迁移到 v3
    assert first["current_version"] == 6


# ---------- 多对多来源关联 ----------

def test_many_evidence_to_one_claim(setup_db):
    result = build_projection(
        _items(), agent_id="agent-a", rule_id="r1", db_path=setup_db,
    )
    prov = claim_provenance(
        result["claims"][0]["claim_id"], agent_id="agent-a", db_path=setup_db
    )
    ids = {e["record_id"] for e in prov["evidence"]}
    assert ids == {REC_MAIN_1, REC_MAIN_2}


def test_one_evidence_to_many_claims(setup_db):
    items = [
        {"claim_key": "k1", "claim_kind": "fact", "content": "事实一",
         "evidence_record_ids": [REC_MAIN_1]},
        {"claim_key": "k2", "claim_kind": "note", "content": "笔记二",
         "evidence_record_ids": [REC_MAIN_1]},
    ]
    result = build_projection(items, agent_id="agent-a", rule_id="r1", db_path=setup_db)
    assert result["created"] == 2
    for c in result["claims"]:
        prov = claim_provenance(c["claim_id"], agent_id="agent-a", db_path=setup_db)
        assert [e["record_id"] for e in prov["evidence"]] == [REC_MAIN_1]


def test_multi_source_fidelity(setup_db):
    """不同 source_kind 的证据汇入一条 Claim，来源逐条保真。"""
    result = build_projection(
        _items(evidence_record_ids=[REC_MAIN_1, REC_PATCH]),
        agent_id="agent-a", rule_id="r1", db_path=setup_db,
    )
    prov = claim_provenance(
        result["claims"][0]["claim_id"], agent_id="agent-a", db_path=setup_db
    )
    kinds = {e["record_id"]: e["source_kind"] for e in prov["evidence"]}
    assert kinds == {REC_MAIN_1: "conversation_export", REC_PATCH: "recent_patch"}
    verified = {e["record_id"]: e["verified"] for e in prov["evidence"]}
    assert verified[REC_PATCH] == 0  # 未核验补丁的 verified 状态原样保留


# ---------- 幂等 ----------

def test_rebuild_is_idempotent(setup_db):
    first = build_projection(_items(), agent_id="agent-a", rule_id="r1", db_path=setup_db)
    second = build_projection(_items(), agent_id="agent-a", rule_id="r1", db_path=setup_db)
    assert second["run_id"] == first["run_id"]
    assert second["created"] == 0 and second["idempotent_replay"] is True
    claims = list_claims(agent_id="agent-a", db_path=setup_db)
    assert len(claims) == 1 and claims[0]["claim_version"] == 1
    with sqlite3.connect(setup_db) as conn:
        links = conn.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM projection_runs").fetchone()[0]
    assert links == 2 and runs == 1


def test_reordered_items_and_evidence_are_same_run(setup_db):
    items = [
        {"claim_key": "k1", "claim_kind": "fact", "content": "事实一",
         "evidence_record_ids": [REC_MAIN_1, REC_MAIN_2]},
        {"claim_key": "k2", "claim_kind": "note", "content": "笔记二",
         "evidence_record_ids": [REC_PATCH]},
    ]
    first = build_projection(
        items, agent_id="agent-a", rule_id="r1", db_path=setup_db
    )
    reordered = [
        dict(items[1]),
        dict(items[0], evidence_record_ids=[REC_MAIN_2, REC_MAIN_1]),
    ]
    second = build_projection(
        reordered, agent_id="agent-a", rule_id="r1", db_path=setup_db
    )
    assert second["run_id"] == first["run_id"]
    assert second["idempotent_replay"] is True
    assert second["created"] == 0 and second["skipped"] == 2


def test_run_id_includes_claim_semantics(setup_db):
    first = build_projection(
        _items(claim_key="k1", content="同一证据的事实一"),
        agent_id="agent-a", rule_id="r1", db_path=setup_db,
    )
    second = build_projection(
        _items(claim_key="k2", content="同一证据的事实二"),
        agent_id="agent-a", rule_id="r1", db_path=setup_db,
    )
    assert second["run_id"] != first["run_id"]
    assert second["created"] == 1 and second["idempotent_replay"] is False


# ---------- 版本化 ----------

def test_source_change_supersedes_without_delete(setup_db):
    first = build_projection(_items(), agent_id="agent-a", rule_id="r1", db_path=setup_db)
    claim_id = first["claims"][0]["claim_id"]
    # 同一 claim_key，证据集变化 → 新版本
    second = build_projection(
        _items(evidence_record_ids=[REC_MAIN_1, REC_MAIN_2, REC_ALT]),
        agent_id="agent-a", rule_id="r1", db_path=setup_db,
    )
    assert second["claims"][0]["claim_version"] == 2
    assert second["superseded"] == 1
    latest = get_claim(claim_id, agent_id="agent-a", db_path=setup_db)
    assert latest["claim_version"] == 2 and latest["status"] == "active"
    v1 = get_claim(claim_id, agent_id="agent-a", version=1, db_path=setup_db)
    assert v1["status"] == "superseded" and v1["superseded_by_version"] == 2
    # 旧版本证据链仍完整可查（历史留痕不删除）
    prov_v1 = claim_provenance(claim_id, agent_id="agent-a", version=1, db_path=setup_db)
    assert {e["record_id"] for e in prov_v1["evidence"]} == {REC_MAIN_1, REC_MAIN_2}


def test_content_change_with_same_evidence_creates_new_version(setup_db):
    first = build_projection(
        _items(content="旧结论"), agent_id="agent-a", rule_id="r1", db_path=setup_db
    )
    claim_id = first["claims"][0]["claim_id"]
    second = build_projection(
        _items(content="新结论"), agent_id="agent-a", rule_id="r1", db_path=setup_db
    )
    assert second["run_id"] != first["run_id"]
    assert second["claims"][0]["claim_version"] == 2
    assert get_claim(claim_id, "agent-a", db_path=setup_db)["content"] == "新结论"
    assert get_claim(claim_id, "agent-a", 1, setup_db)["content"] == "旧结论"


def test_rule_version_change_creates_auditable_claim_version(setup_db):
    first = build_projection(
        _items(), agent_id="agent-a", rule_id="r1", rule_version=1,
        db_path=setup_db,
    )
    second = build_projection(
        _items(), agent_id="agent-a", rule_id="r1", rule_version=2,
        db_path=setup_db,
    )
    assert second["run_id"] != first["run_id"]
    assert second["claims"][0]["claim_version"] == 2
    claim = get_claim(
        second["claims"][0]["claim_id"], "agent-a", db_path=setup_db
    )
    assert claim["rule_version"] == 2


# ---------- agent 隔离（不可绕行） ----------

def test_agent_isolation_read_and_list(setup_db):
    result = build_projection(_items(), agent_id="agent-b", rule_id="r1", db_path=setup_db)
    claim_id = result["claims"][0]["claim_id"]
    assert get_claim(claim_id, agent_id="agent-a", db_path=setup_db) is None
    assert list_claims(agent_id="agent-a", db_path=setup_db) == []
    assert claim_provenance(claim_id, agent_id="agent-a", db_path=setup_db) is None
    assert get_claim(claim_id, agent_id="agent-b", db_path=setup_db) is not None


def test_same_claim_key_different_agents_are_distinct(setup_db):
    ra = build_projection(_items(), agent_id="agent-a", rule_id="r1", db_path=setup_db)
    rb = build_projection(_items(), agent_id="agent-b", rule_id="r1", db_path=setup_db)
    assert ra["claims"][0]["claim_id"] != rb["claims"][0]["claim_id"]


def test_claim_identity_has_no_delimiter_collision(setup_db):
    # M5-04：ad-hoc agent 也须先注册才过可见性闸门的 active 校验
    register_agent("a|b", "合成左", actor="test", db_path=setup_db)
    register_agent("a", "合成右", actor="test", db_path=setup_db)
    left = build_projection(
        _items(claim_key="c"), agent_id="a|b", rule_id="r1", db_path=setup_db
    )
    right = build_projection(
        _items(claim_key="b|c"), agent_id="a", rule_id="r1", db_path=setup_db
    )
    assert left["claims"][0]["claim_id"] != right["claims"][0]["claim_id"]


@pytest.mark.parametrize("bad_agent", [None, "", "   "])
def test_empty_agent_id_rejected_everywhere(setup_db, bad_agent):
    with pytest.raises(ValueError):
        build_projection(_items(), agent_id=bad_agent, rule_id="r1", db_path=setup_db)
    with pytest.raises(ValueError):
        get_claim("clm-x", agent_id=bad_agent, db_path=setup_db)
    with pytest.raises(ValueError):
        list_claims(agent_id=bad_agent, db_path=setup_db)
    with pytest.raises(ValueError):
        claim_provenance("clm-x", agent_id=bad_agent, db_path=setup_db)


@pytest.mark.parametrize(
    "bad_item",
    [
        {"claim_key": "", "claim_kind": "fact", "content": "x",
         "evidence_record_ids": [REC_MAIN_1]},
        {"claim_key": "k", "claim_kind": "future-kind", "content": "x",
         "evidence_record_ids": [REC_MAIN_1]},
        {"claim_key": "k", "claim_kind": "fact", "content": "   ",
         "evidence_record_ids": [REC_MAIN_1]},
        {"claim_key": "k", "claim_kind": "fact", "content": "x",
         "evidence_record_ids": REC_MAIN_1},
        {"claim_key": "k", "claim_kind": "fact", "content": "x",
         "evidence_record_ids": []},
    ],
)
def test_invalid_projection_items_fail_before_writing(setup_db, bad_item):
    with pytest.raises(ValueError):
        build_projection(
            [bad_item], agent_id="agent-a", rule_id="r1", db_path=setup_db
        )
    with sqlite3.connect(setup_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM projection_runs").fetchone()[0] == 0


def test_duplicate_claim_key_in_one_run_is_rejected(setup_db):
    duplicate = _items() + _items(content="同一键的另一份输出")
    with pytest.raises(ValueError, match="claim_key"):
        build_projection(
            duplicate, agent_id="agent-a", rule_id="r1", db_path=setup_db
        )


@pytest.mark.parametrize("bad_version", [0, -1, 1.5, True, "1"])
def test_invalid_rule_version_is_rejected(setup_db, bad_version):
    with pytest.raises(ValueError, match="rule_version"):
        build_projection(
            _items(), agent_id="agent-a", rule_id="r1",
            rule_version=bad_version, db_path=setup_db,
        )


def test_invalid_claim_status_is_rejected(setup_db):
    with pytest.raises(ValueError, match="status"):
        list_claims(agent_id="agent-a", status="all", db_path=setup_db)


# ---------- 事务回滚 ----------

def test_rollback_on_missing_evidence(setup_db):
    items = _items() + [{
        "claim_key": "bad", "claim_kind": "fact", "content": "坏引用",
        "evidence_record_ids": ["no-such-record-000"],
    }]
    with pytest.raises(ValueError):
        build_projection(items, agent_id="agent-a", rule_id="r1", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM projection_claims").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM projection_runs").fetchone()[0] == 0


def test_rollback_on_invalid_item_mid_batch(setup_db):
    items = [
        {"claim_key": "good", "claim_kind": "fact", "content": "好条目",
         "evidence_record_ids": [REC_MAIN_1]},
        {"claim_key": "bad", "claim_kind": "fact", "content": "无证据",
         "evidence_record_ids": []},
    ]
    with pytest.raises(ValueError):
        build_projection(items, agent_id="agent-a", rule_id="r1", db_path=setup_db)
    assert list_claims(agent_id="agent-a", db_path=setup_db) == []


# ---------- 证据层不可变 ----------

def test_evidence_untouched_by_projection(setup_db):
    before = _evidence_hashes(setup_db)
    build_projection(_items(), agent_id="agent-a", rule_id="r1", db_path=setup_db)
    build_projection(
        _items(evidence_record_ids=[REC_MAIN_1, REC_MAIN_2, REC_ALT]),
        agent_id="agent-a", rule_id="r1", db_path=setup_db,
    )
    after = _evidence_hashes(setup_db)
    assert before == after  # 证据内容哈希逐条不变：不覆盖、不合并、不删除


def test_evidence_table_rejects_direct_update_and_delete(setup_db):
    with sqlite3.connect(setup_db) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE records_v1 SET content = ? WHERE record_id = ?",
                ("被篡改", REC_MAIN_1),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM records_v1 WHERE record_id = ?", (REC_MAIN_1,))
        conn.rollback()
        assert conn.execute(
            "SELECT COUNT(*) FROM records_v1 WHERE record_id = ?", (REC_MAIN_1,)
        ).fetchone()[0] == 1


def test_projection_integrity_v6_allows_versioning_and_blocks_tamper(setup_db):
    first = build_projection(
        _items(), agent_id="agent-a", rule_id="r1", db_path=setup_db
    )
    first_claim = first["claims"][0]

    # 合法路径仍可把旧 active 版本标记为 superseded 并追加新版本。
    second = build_projection(
        _items(content="第二版内容"),
        agent_id="agent-a",
        rule_id="r1",
        db_path=setup_db,
    )
    assert second["created"] == 1
    assert second["superseded"] == 1

    with sqlite3.connect(setup_db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        row = conn.execute(
            "SELECT id, status, superseded_by_version FROM projection_claims "
            "WHERE claim_id = ? AND claim_version = ?",
            (first_claim["claim_id"], first_claim["claim_version"]),
        ).fetchone()
        assert row[1:] == ("superseded", 2)

        # Claim 身份、正文、来源与历史行均不可原地篡改或删除。
        for sql in (
            "UPDATE projection_claims SET content = 'tampered' WHERE id = ?",
            "UPDATE projection_claims SET source_hash = printf('%064d', 0) WHERE id = ?",
            "UPDATE projection_claims SET status = 'sealed' WHERE id = ?",
            "DELETE FROM projection_claims WHERE id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, (row[0],))
            conn.rollback()

        evidence = conn.execute(
            "SELECT claim_rowid, record_rowid, link_kind FROM claim_evidence "
            "ORDER BY claim_rowid LIMIT 1"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE claim_evidence SET link_kind = 'contradicts' "
                "WHERE claim_rowid = ? AND record_rowid = ? AND link_kind = ?",
                evidence,
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM claim_evidence WHERE claim_rowid = ? "
                "AND record_rowid = ? AND link_kind = ?",
                evidence,
            )
        conn.rollback()

        run_id = conn.execute(
            "SELECT run_id FROM projection_runs ORDER BY created_at LIMIT 1"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE projection_runs SET claim_count = 99 WHERE run_id = ?",
                (run_id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM projection_runs WHERE run_id = ?", (run_id,))
        conn.rollback()

        membership = conn.execute(
            "SELECT record_rowid, branch_id, position "
            "FROM records_v1_branch_memberships ORDER BY record_rowid LIMIT 1"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE records_v1_branch_memberships SET position = 999 "
                "WHERE record_rowid = ? AND branch_id = ?",
                membership[:2],
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM records_v1_branch_memberships "
                "WHERE record_rowid = ? AND branch_id = ?",
                membership[:2],
            )
        conn.rollback()


def test_migration_v6_failure_rolls_back_only_v6(tmp_path, monkeypatch):
    db_path = tmp_path / "projection-v6-migration-failure.db"
    migrations = (
        records_module.MIGRATION_1_STATEMENTS,
        records_module.MIGRATION_2_STATEMENTS,
        records_module.MIGRATION_3_STATEMENTS,
        records_module.MIGRATION_4_STATEMENTS,
        records_module.MIGRATION_5_STATEMENTS,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for statements in migrations:
            for statement in statements:
                conn.execute(statement)
        conn.executemany(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            [(version, f"v{version}", "synthetic") for version in range(1, 6)],
        )

    monkeypatch.setattr(
        records_module,
        "MIGRATION_6_STATEMENTS",
        records_module.MIGRATION_6_STATEMENTS + ("THIS IS NOT VALID SQL",),
    )
    with pytest.raises(sqlite3.OperationalError):
        migrate_records_db(str(db_path))

    with sqlite3.connect(db_path) as conn:
        versions = [
            row[0] for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        v6_triggers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name IN ("
            "'projection_runs_immutable_update', "
            "'projection_runs_immutable_delete', "
            "'projection_claims_guard_update', "
            "'projection_claims_immutable_delete', "
            "'claim_evidence_immutable_update', "
            "'claim_evidence_immutable_delete')"
        ).fetchone()[0]
    assert versions == [1, 2, 3, 4, 5]
    assert v6_triggers == 0


def test_migration_v6_rejects_inconsistent_existing_projection(tmp_path):
    db_path = tmp_path / "projection-v6-inconsistent.db"
    migrations = (
        records_module.MIGRATION_1_STATEMENTS,
        records_module.MIGRATION_2_STATEMENTS,
        records_module.MIGRATION_3_STATEMENTS,
        records_module.MIGRATION_4_STATEMENTS,
        records_module.MIGRATION_5_STATEMENTS,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for statements in migrations:
            for statement in statements:
                conn.execute(statement)
        conn.executemany(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            [(version, f"v{version}", "synthetic") for version in range(1, 6)],
        )
        conn.execute(
            "INSERT INTO projection_runs "
            "(run_id, rule_id, rule_version, agent_id, source_hash, claim_count, "
            "status, created_at) VALUES ('run-bad', 'rule', 1, 'agt-legacy', ?, 1, "
            "'completed', 'synthetic')",
            ("0" * 64,),
        )
        conn.execute(
            "INSERT INTO projection_claims "
            "(claim_id, claim_version, agent_id, claim_kind, content, status, "
            "rule_id, rule_version, source_hash, projection_hash, run_id, created_at, "
            "superseded_by_version) VALUES "
            "('claim-bad', 1, 'agt-legacy', 'fact', 'synthetic', 'superseded', "
            "'rule', 1, ?, ?, 'run-bad', 'synthetic', NULL)",
            ("0" * 64, "1" * 64),
        )

    with pytest.raises(sqlite3.IntegrityError, match="inconsistent"):
        migrate_records_db(str(db_path))
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 6"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'projection_%immutable%'"
        ).fetchone()[0] == 0


def test_migration_v3_failure_rolls_back_only_v3(tmp_path, monkeypatch):
    db_path = tmp_path / "projection-migration-failure.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for statement in records_module.MIGRATION_1_STATEMENTS:
            conn.execute(statement)
        for statement in records_module.MIGRATION_2_STATEMENTS:
            conn.execute(statement)
        conn.executemany(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            [(1, "v1", "synthetic"), (2, "v2", "synthetic")],
        )
    monkeypatch.setattr(
        records_module,
        "MIGRATION_3_STATEMENTS",
        records_module.MIGRATION_3_STATEMENTS + ("THIS IS NOT VALID SQL",),
    )
    with pytest.raises(sqlite3.OperationalError):
        migrate_records_db(str(db_path))
    with sqlite3.connect(db_path) as conn:
        versions = [
            row[0] for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        projection_tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name LIKE 'projection_%'"
        ).fetchone()[0]
        assert versions == [1, 2]
        assert projection_tables == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'records_v1'"
        ).fetchone()[0] == 1
