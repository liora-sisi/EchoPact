"""M5-06 收口测试：可见性谓词单一来源的一致性锁（纯合成，不联网）。

背景：M5-05 互审观察——owner/scope/grants 判定曾双写于 can_read_record
与 _can_read_derived，存在静默分叉风险。M5-06 将其收敛为 identity 模块的
_read_channel 单一判定源（布尔结论与 via 通道分类同源）。

本文件不调用内部谓词做"自己比自己"的同义反复，而是用一张手工真值表
对三条公开读取路径（can_read_record / filter_visible_records /
all_visible_rowids）和审计 via/can_read 输出做矩阵断言：将来任何一处
再分叉，这里立刻炸响。
"""
import json
import sqlite3
from contextlib import closing

import pytest

import backend.utils.db as db_module
from backend.memory import audit
from backend.memory.identity import (
    IdentityError,
    all_visible_rowids,
    can_read_record,
    filter_visible_records,
    grant_access,
    register_agent,
    revoke_access,
    set_agent_enabled,
    set_owner,
    set_scope,
)
from backend.memory.records_v1 import import_record_package

OWNER = "agt-m6-owner"
PEER = "agt-m6-peer"
STRANGER = "agt-m6-stranger"
AGENTS = (OWNER, PEER, STRANGER)

R1, R2, R3, R4, R5 = "m6-r1", "m6-r2", "m6-r3", "m6-r4", "m6-r5"
RECORDS = (R1, R2, R3, R4, R5)

# 手工真值表：record -> agent -> (can_read, via)
# r1 私有 + 授权 peer；r2 共享 + 授权 peer（验通道优先级 scope_shared > grant）；
# r3 私有无任何授权；r4 授权 stranger 后撤销（epoch 内失效）；
# r5 归属转移给 peer（旧 owner 立即失权）。
EXPECTED = {
    R1: {OWNER: (True, "owner"), PEER: (True, "grant"), STRANGER: (False, "none")},
    R2: {OWNER: (True, "owner"), PEER: (True, "scope_shared"),
         STRANGER: (True, "scope_shared")},
    R3: {OWNER: (True, "owner"), PEER: (False, "none"), STRANGER: (False, "none")},
    R4: {OWNER: (True, "owner"), PEER: (False, "none"), STRANGER: (False, "none")},
    R5: {OWNER: (False, "none"), PEER: (True, "owner"), STRANGER: (False, "none")},
}


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "m506.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    return str(db_path)


def _record(record_id):
    return {
        "record_id": record_id,
        "source_kind": "conversation_export",
        "source_ref": f"synthetic://m506/{record_id}",
        "conversation_id": "synthetic-m506",
        "branch_id": "main",
        "message_id": record_id,
        "role": "user",
        "content": f"谓词一致性合成内容 {record_id}",
        "created_at": "2026-07-01T00:00:00Z",
        "verified": True,
        "authority": "user-confirmed",
        "source_cutoff_at": "2026-08-01T00:00:00Z",
        "conflict_group_id": None,
    }


@pytest.fixture
def seeded(setup_db, tmp_path):
    for agent_id in AGENTS:
        register_agent(agent_id, f"合成 {agent_id}", actor="test",
                       db_path=setup_db)
    pkg = tmp_path / "pkg.json"
    pkg.write_text(
        json.dumps({"schema_version": "echo-pact-records-v1",
                    "records": [_record(r) for r in RECORDS]}, ensure_ascii=False),
        encoding="utf-8",
    )
    import_record_package(str(pkg), db_path=setup_db, owner_agent_id=OWNER)
    grant_access(R1, PEER, actor="馆长", db_path=setup_db)
    set_scope(R2, "shared", actor="馆长", db_path=setup_db)
    grant_access(R2, PEER, actor="馆长", db_path=setup_db)
    grant_access(R4, STRANGER, actor="馆长", db_path=setup_db)
    grant_seq = next(
        e["event_seq"] for e in audit.list_events(setup_db, R4)["events"]
        if e["event_kind"] == "grant"
    )
    revoke_access(R4, STRANGER, grant_seq, actor="馆长", db_path=setup_db)
    set_owner(R5, PEER, actor="馆长", db_path=setup_db)
    return setup_db


def _rowids(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return {
            row["record_id"]: row["id"]
            for row in conn.execute(
                "SELECT id, record_id FROM records_v1"
            ).fetchall()
        }


def test_three_read_paths_agree_with_truth_table(seeded):
    rowids = _rowids(seeded)
    all_rowids = list(rowids.values())
    with closing(sqlite3.connect(seeded)) as conn:
        conn.row_factory = sqlite3.Row
        for record_id in RECORDS:
            rowid = rowids[record_id]
            batch = filter_visible_records(conn, OWNER, all_rowids)
            recall_set = all_visible_rowids(conn, OWNER)
            for agent_id in AGENTS:
                expected_can, _ = EXPECTED[record_id][agent_id]
                single = can_read_record(conn, agent_id, rowid)
                in_batch = rowid in filter_visible_records(
                    conn, agent_id, all_rowids)
                in_recall = rowid in all_visible_rowids(conn, agent_id)
                assert single == expected_can, (record_id, agent_id, "single")
                assert in_batch == expected_can, (record_id, agent_id, "batch")
                assert in_recall == expected_can, (record_id, agent_id, "recall")
        # 防止上面的逐 agent 查询掩盖 batch/recall 本身的形状错误
        assert batch == {rowids[r] for r in RECORDS if EXPECTED[r][OWNER][0]}
        assert recall_set == batch


def test_audit_via_and_can_read_match_truth_table(seeded):
    for record_id in RECORDS:
        for agent_id in AGENTS:
            expected_can, expected_via = EXPECTED[record_id][agent_id]
            check = audit.who_can_read(
                seeded, record_id, agent_id=agent_id)["agent_check"]
            assert check["can_read"] is expected_can, (record_id, agent_id)
            assert check["via"] == expected_via, (record_id, agent_id)
            assert check["agent_state"] == "active"


def test_disabled_agent_consistent_across_paths(seeded):
    rowids = _rowids(seeded)
    all_rowids = list(rowids.values())
    set_agent_enabled(PEER, False, actor="馆长", db_path=seeded)
    with closing(sqlite3.connect(seeded)) as conn:
        conn.row_factory = sqlite3.Row
        for record_id in RECORDS:
            with pytest.raises(IdentityError):
                can_read_record(conn, PEER, rowids[record_id])
        with pytest.raises(IdentityError):
            filter_visible_records(conn, PEER, all_rowids)
        with pytest.raises(IdentityError):
            all_visible_rowids(conn, PEER)
    # 审计侧与正式路径一样失败关闭，但 via 仍如实报告命中通道
    check = audit.who_can_read(seeded, R1, agent_id=PEER)["agent_check"]
    assert check["can_read"] is False
    assert check["via"] == "grant"
    assert check["agent_state"] == "disabled"
    # 恢复后三条路径同时复活
    set_agent_enabled(PEER, True, actor="馆长", db_path=seeded)
    with closing(sqlite3.connect(seeded)) as conn:
        conn.row_factory = sqlite3.Row
        assert can_read_record(conn, PEER, rowids[R1]) is True
        assert rowids[R1] in filter_visible_records(conn, PEER, all_rowids)
        assert rowids[R1] in all_visible_rowids(conn, PEER)
