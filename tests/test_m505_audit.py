"""M5-05 只读审计 CLI + 身份全流程彩排验收测试（纯合成，不联网）。

覆盖工单：
- who-can-read / list-events / agent-status 三件套的正确性与脱敏；
- 退出码：0 成功 / 2 输入错误 / 3 环境错误；
- 只读证明：审计前后库文件逐字节不变，且绝不迁移旧库；
- 彩排：十步全过、报告脱敏、断网可跑、失败路径退出码 1。
"""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import backend.utils.db as db_module
from backend.memory import audit
from backend.memory.audit import (
    AuditEnvironmentError,
    AuditNotFound,
    agent_status,
    list_events,
    who_can_read,
)
from backend.memory.identity import (
    grant_access,
    issue_credential,
    register_agent,
    revoke_access,
    rotate_credential,
    set_agent_enabled,
    set_owner,
)
from backend.memory.records_v1 import import_record_package
from scripts import audit_cli
from scripts import rehearsal_identity


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "m505.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    return str(db_path)


def _record(record_id, created_at="2026-07-01T00:00:00Z"):
    return {
        "record_id": record_id,
        "source_kind": "conversation_export",
        "source_ref": f"synthetic://m505/{record_id}",
        "conversation_id": "synthetic-m505",
        "branch_id": "main",
        "message_id": record_id,
        "role": "user",
        "content": f"彩排审计合成内容 {record_id}",
        "created_at": created_at,
        "verified": True,
        "authority": "user-confirmed",
        "source_cutoff_at": "2026-08-01T00:00:00Z",
        "conflict_group_id": None,
    }


def _seed(setup_db, tmp_path):
    """合成场景：owner 三条记录；R1 授权 peer；R2 易主 peer；R3 授权后撤销。"""
    for agent_id in ("agt-owner", "agt-peer", "agt-stranger"):
        register_agent(agent_id, f"合成 {agent_id}", actor="test",
                       db_path=setup_db)
    pkg = tmp_path / "pkg.json"
    pkg.write_text(
        json.dumps({"schema_version": "echo-pact-records-v1",
                    "records": [_record("m505-r1"), _record("m505-r2"),
                                _record("m505-r3")]}, ensure_ascii=False),
        encoding="utf-8",
    )
    import_record_package(str(pkg), db_path=setup_db, owner_agent_id="agt-owner")
    grant_access("m505-r1", "agt-peer", actor="馆长", db_path=setup_db)
    grant_access("m505-r2", "agt-stranger", actor="馆长", db_path=setup_db)
    set_owner("m505-r2", "agt-peer", actor="馆长", db_path=setup_db)
    grant_access("m505-r3", "agt-stranger", actor="馆长", db_path=setup_db)
    events = list_events(setup_db, "m505-r3")["events"]
    grant_seq = next(e["event_seq"] for e in events
                     if e["event_kind"] == "grant")
    revoke_access("m505-r3", "agt-stranger", grant_seq,
                  actor="馆长", db_path=setup_db)


# ---------- who-can-read ----------

def test_who_can_read_derivation(setup_db, tmp_path):
    _seed(setup_db, tmp_path)
    vis = who_can_read(setup_db, "m505-r1")
    assert vis["owner"] == "agt-owner"
    assert vis["scope"] == "private"
    assert vis["grants"] == ["agt-peer"]
    assert vis["epoch"] > 0
    assert vis["private_epoch"] == 0
    assert len(vis["content_fingerprint"]) == 12
    # 易主后的 R2：epoch 刷新、旧授权作废
    vis2 = who_can_read(setup_db, "m505-r2")
    assert vis2["owner"] == "agt-peer"
    assert vis2["grants"] == []
    assert vis2["epoch"] > vis["epoch"]


def test_who_can_read_agent_check(setup_db, tmp_path):
    _seed(setup_db, tmp_path)
    owner = who_can_read(setup_db, "m505-r1", agent_id="agt-owner")["agent_check"]
    assert owner["can_read"] is True and owner["via"] == "owner"
    peer = who_can_read(setup_db, "m505-r1", agent_id="agt-peer")["agent_check"]
    assert peer["can_read"] is True and peer["via"] == "grant"
    stranger = who_can_read(
        setup_db, "m505-r1", agent_id="agt-stranger")["agent_check"]
    assert stranger["can_read"] is False and stranger["via"] == "none"
    # scope_shared 下停用 agent 仍不可读
    from backend.memory.identity import set_scope
    set_scope("m505-r1", "shared", actor="馆长", db_path=setup_db)
    set_agent_enabled("agt-stranger", False, actor="馆长", db_path=setup_db)
    disabled = who_can_read(
        setup_db, "m505-r1", agent_id="agt-stranger")["agent_check"]
    assert disabled["via"] == "scope_shared"
    assert disabled["can_read"] is False
    assert disabled["agent_state"] == "disabled"


def test_who_can_read_disabled_owner_fails_closed(setup_db, tmp_path):
    _seed(setup_db, tmp_path)
    set_agent_enabled("agt-owner", False, actor="馆长", db_path=setup_db)
    owner = who_can_read(
        setup_db, "m505-r1", agent_id="agt-owner"
    )["agent_check"]
    assert owner == {
        "agent_id": "agt-owner",
        "agent_state": "disabled",
        "can_read": False,
        "via": "owner",
    }


# ---------- list-events ----------

def test_list_events_stream(setup_db, tmp_path):
    _seed(setup_db, tmp_path)
    result = list_events(setup_db, "m505-r3")
    kinds = [e["event_kind"] for e in result["events"]]
    assert kinds == ["set_owner", "grant", "revoke"]
    seqs = [e["event_seq"] for e in result["events"]]
    assert seqs == sorted(seqs)
    revoke = result["events"][-1]
    assert revoke["target_event_seq"] == result["events"][1]["event_seq"]
    assert revoke["actor"] == "馆长"
    assert result["event_count"] == 3


# ---------- agent-status ----------

def test_agent_status_lifecycle_and_credentials(setup_db, tmp_path):
    _seed(setup_db, tmp_path)
    cred = issue_credential("agt-peer", actor="馆长", db_path=setup_db)
    rotated = rotate_credential(cred["cred_id"], actor="馆长", db_path=setup_db)
    status = agent_status(setup_db, "agt-peer")
    assert status["state"] == "active"
    assert [e["kind"] for e in status["lifecycle_events"]] == ["registered"]
    creds = {c["cred_id"]: c for c in status["credentials"]}
    assert creds[cred["cred_id"]]["state"] == "rotated"
    assert creds[cred["cred_id"]]["replacement_cred_id"] == rotated["cred_id"]
    assert creds[cred["cred_id"]]["grace_until"] is not None
    assert creds[rotated["cred_id"]]["state"] == "active"
    # 停用 / 恢复
    set_agent_enabled("agt-peer", False, actor="馆长", db_path=setup_db)
    assert agent_status(setup_db, "agt-peer")["state"] == "disabled"
    set_agent_enabled("agt-peer", True, actor="馆长", db_path=setup_db)
    final = agent_status(setup_db, "agt-peer")
    assert final["state"] == "active"
    assert [e["kind"] for e in final["lifecycle_events"]] == [
        "registered", "disabled", "re-enabled",
    ]


# ---------- 退出码与异常输入 ----------

def test_not_found_exit_2(setup_db, tmp_path):
    _seed(setup_db, tmp_path)
    with pytest.raises(AuditNotFound):
        who_can_read(setup_db, "no-such-record")
    with pytest.raises(AuditNotFound):
        list_events(setup_db, "no-such-record")
    with pytest.raises(AuditNotFound):
        agent_status(setup_db, "agt-ghost")
    with pytest.raises(AuditNotFound):
        who_can_read(setup_db, "m505-r1", agent_id="   ")
    assert audit_cli.main(
        ["--db-path", setup_db, "who-can-read", "no-such-record"]) == 2
    assert audit_cli.main(
        ["--db-path", setup_db, "agent-status", "agt-ghost"]) == 2


def test_environment_exit_3(setup_db, tmp_path):
    assert audit_cli.main(
        ["--db-path", str(tmp_path / "missing.db"), "agent-status", "x"]) == 3
    # 未迁移到 v5 的旧库：只读拒绝，绝不顺手迁移
    old_db = tmp_path / "old.db"
    sqlite3.connect(str(old_db)).execute(
        "CREATE TABLE records_v1 (id INTEGER PRIMARY KEY)")
    with pytest.raises(AuditEnvironmentError):
        agent_status(str(old_db), "x")
    assert audit_cli.main(
        ["--db-path", str(old_db), "agent-status", "x"]) == 3
    with sqlite3.connect(str(old_db)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "agents" not in tables  # 审计工具没有写任何表


def test_readonly_uri_escapes_special_filename(setup_db, tmp_path):
    special_db = tmp_path / "m505 # readonly.db"
    _seed(str(special_db), tmp_path)
    result = who_can_read(str(special_db), "m505-r1", agent_id="agt-peer")
    assert result["agent_check"]["can_read"] is True


def test_v5_marker_is_required_even_when_tables_exist(setup_db, tmp_path):
    _seed(setup_db, tmp_path)
    with sqlite3.connect(setup_db) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = 5")
    with pytest.raises(AuditEnvironmentError, match="缺少版本 5 迁移记录"):
        who_can_read(setup_db, "m505-r1")


def test_success_exit_0(setup_db, tmp_path, capsys):
    _seed(setup_db, tmp_path)
    assert audit_cli.main(
        ["--db-path", setup_db, "who-can-read", "m505-r1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["owner"] == "agt-owner"


# ---------- 只读证明与脱敏 ----------

def test_audit_is_byte_for_byte_readonly(setup_db, tmp_path):
    _seed(setup_db, tmp_path)
    before = hashlib.sha256(Path(setup_db).read_bytes()).hexdigest()
    who_can_read(setup_db, "m505-r1", agent_id="agt-peer")
    list_events(setup_db, "m505-r2")
    agent_status(setup_db, "agt-owner")
    after = hashlib.sha256(Path(setup_db).read_bytes()).hexdigest()
    assert before == after
    # 只读模式不产生 wal/shm 副产物
    assert not Path(setup_db + "-wal").exists()
    assert not Path(setup_db + "-shm").exists()


def _walk_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def test_audit_output_redaction(setup_db, tmp_path):
    """审计输出：字段名不见禁词，字段值不见真实凭证材料与记录正文。"""
    _seed(setup_db, tmp_path)
    cred = issue_credential("agt-owner", actor="馆长", db_path=setup_db)
    outputs = [
        who_can_read(setup_db, "m505-r1", agent_id="agt-peer"),
        list_events(setup_db, "m505-r1"),
        agent_status(setup_db, "agt-owner"),
    ]
    for out in outputs:
        keys = set(_walk_keys(out))
        for forbidden in audit.FORBIDDEN_AUDIT_FIELDS:
            assert forbidden not in keys
        blob = json.dumps(out, ensure_ascii=False)
        assert cred["secret"] not in blob
        assert cred["token"] not in blob
        assert "彩排审计合成内容" not in blob  # 记录正文不进审计输出


# ---------- 彩排 ----------

def test_rehearsal_full_pass(setup_db, tmp_path):
    out = tmp_path / "report.json"
    assert rehearsal_identity.main(["--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_version"] == "echo-pact-m505-rehearsal-v1"
    assert report["summary"] == {"total": 10, "passed": 10, "failed": 0}
    assert [s["step"] for s in report["steps"]] == [
        "01-注册", "02-签发", "03-导入", "04-授权", "05-归属转移",
        "06-撤销", "07-召回", "08-投影", "09-冲突呈现", "10-脱敏审计报告",
    ]
    assert all(s["status"] == "pass" for s in report["steps"])


def test_rehearsal_report_redaction(setup_db, tmp_path):
    out = tmp_path / "report.json"
    rehearsal_identity.main(["--out", str(out)])
    blob = out.read_text(encoding="utf-8")
    assert "彩排灯塔" not in blob  # 记录正文
    report = json.loads(blob)
    keys = set(_walk_keys(report))
    for forbidden in ("secret", "secret_hash", "salt_hex", "params_json"):
        assert forbidden not in keys


def test_rehearsal_runs_without_network(setup_db, tmp_path, monkeypatch):
    import socket

    def no_network(*args, **kwargs):
        raise AssertionError("彩排不得联网")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    out = tmp_path / "offline-report.json"
    assert rehearsal_identity.main(["--out", str(out)]) == 0


def test_rehearsal_cleans_temporary_database(setup_db, tmp_path, monkeypatch):
    """临时目录自清理且不修改进程级 tempfile 状态。

    只替换彩排模块自己的 TemporaryDirectory 绑定，把本轮目录圈进该测试
    的独立沙盒；不会影响其他线程/测试，也不会扫描并发进程的系统临时目录。
    """
    temp_root = tmp_path / "rehearsal-temp-root"
    temp_root.mkdir()
    real_temporary_directory = rehearsal_identity.TemporaryDirectory

    def isolated_temporary_directory(*args, **kwargs):
        kwargs["dir"] = str(temp_root)
        return real_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(
        rehearsal_identity, "TemporaryDirectory", isolated_temporary_directory
    )
    out = tmp_path / "cleanup-report.json"
    assert rehearsal_identity.main(["--out", str(out)]) == 0
    assert list(temp_root.iterdir()) == []


def test_rehearsal_refuses_to_overwrite_report(setup_db, tmp_path):
    out = tmp_path / "existing-report.json"
    original = b"keep-this-report-byte-for-byte"
    out.write_bytes(original)
    assert rehearsal_identity.main(["--out", str(out)]) == 2
    assert out.read_bytes() == original


def test_rehearsal_failure_path_exit_1(setup_db, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic rehearsal sabotage")

    monkeypatch.setattr(rehearsal_identity, "register_agent", boom)
    out = tmp_path / "fail-report.json"
    assert rehearsal_identity.main(["--out", str(out)]) == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"]["failed"] > 0
    assert report["steps"][0]["status"] == "fail"
    assert "synthetic rehearsal sabotage" in json.dumps(
        report["steps"][0], ensure_ascii=False)
