"""M5-04 身份与证据可见范围验收测试（纯合成数据，不联网、不读真实数据）。

20 项 = v3 设计 15 项验收清单 + 终审 5 条施工裁定的回归：
- 导入批次绑定/续跑/重放/不自授权（裁定 1、2；清单 1、2、3、13）
- 凭证生命周期/轮换/终态/时间侧信道（清单 6、9、10）
- 可见性推导：legacy 兜底、scope、epoch、grant/revoke（清单 7、8、14；裁定 1、3、4）
- 召回 ACL 先于 LIMIT、coverage 同口径、restricted 脱敏、投影闸门
  （清单 4、5、11、12；裁定 5）
- 静态扫描（清单 15）
"""
import json
import sqlite3
from pathlib import Path

import pytest

import backend.utils.db as db_module
import backend.memory.identity as identity_module
from backend.memory.identity import (
    LEGACY_PRINCIPAL,
    IdentityError,
    all_visible_rowids,
    can_read_record,
    current_visibility,
    grant_access,
    issue_credential,
    register_agent,
    revoke_access,
    revoke_credential,
    rotate_credential,
    set_agent_enabled,
    set_owner,
    set_scope,
    verify_credential,
)
from backend.memory.claim_conflicts import register_conflict
from backend.memory.projection import build_projection
from backend.memory.recall_projection import recall_with_projection
from backend.memory.records_v1 import (
    RECORD_SCHEMA_VERSION,
    import_record_package,
    migrate_records_db,
    recall_records,
)


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "m504.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    return str(db_path)


def _write_package(tmp_path, records, name="pkg.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {"schema_version": RECORD_SCHEMA_VERSION, "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(path)


def _record(record_id, content, created_at, verified=True):
    return {
        "record_id": record_id,
        "source_kind": "conversation_export",
        "source_ref": f"synthetic://m504/{record_id}",
        "conversation_id": "synthetic-m504",
        "branch_id": "main",
        "message_id": record_id,
        "role": "user",
        "content": content,
        "created_at": created_at,
        "verified": verified,
        "authority": "user-confirmed",
        "source_cutoff_at": "2026-08-01T00:00:00Z",
        "conflict_group_id": None,
    }


def _register(*agent_ids, db_path):
    for agent_id in agent_ids:
        register_agent(agent_id, f"合成 {agent_id}", actor="test", db_path=db_path)


def _batch_status(db_path, batch_id):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM import_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return row[0] if row else None


# ---------- A. 导入批次（裁定 1、2；清单 1、2、3、13） ----------

def test_import_does_not_self_grant_actor(setup_db, tmp_path):
    """清单 1 + 裁定 1：actor 只是审计归因；导入不给 actor/他人任何授权。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    pkg = _write_package(tmp_path, [
        _record("m504-a-001", "灯塔计划合成证据一", "2026-07-01T00:00:00Z"),
        _record("m504-a-002", "灯塔计划合成证据二", "2026-07-02T00:00:00Z"),
    ])
    summary = import_record_package(
        pkg, db_path=setup_db, owner_agent_id="agt-a", actor="馆长"
    )
    assert summary["added"] == 2
    with sqlite3.connect(setup_db) as conn:
        conn.row_factory = sqlite3.Row
        events = conn.execute(
            "SELECT event_kind, target_agent, actor FROM record_visibility_events"
        ).fetchall()
        # 每条记录一条 set_owner：actor=馆长（归因），target=agt-a（归属）
        assert len(events) == 2
        for event in events:
            assert event["event_kind"] == "set_owner"
            assert event["target_agent"] == "agt-a"
            assert event["actor"] == "馆长"
        # 不存在任何 grant：actor 与旁观者都未被静默授权
        assert not any(e["event_kind"] == "grant" for e in events)
        rowid = conn.execute(
            "SELECT id FROM records_v1 WHERE record_id = 'm504-a-001'"
        ).fetchone()["id"]
        assert can_read_record(conn, "agt-a", rowid) is True
        assert can_read_record(conn, "agt-b", rowid) is False
        # actor 字符串不是 agent，can_read 一律先校验 active
        with pytest.raises(IdentityError):
            can_read_record(conn, "馆长", rowid)


def test_unimplemented_grant_policy_fails_before_batch_write(setup_db, tmp_path):
    """A recorded-but-unapplied policy must never pretend an ACL was created."""
    _register("agt-a", "agt-b", db_path=setup_db)
    pkg = _write_package(tmp_path, [
        _record("m504-policy-001", "灯塔计划策略证据", "2026-07-01T00:00:00Z"),
    ])
    with pytest.raises(ValueError, match="grant_policy 尚未实现"):
        import_record_package(
            pkg,
            db_path=setup_db,
            owner_agent_id="agt-a",
            actor="馆长",
            batch_id="imp-policy",
            grant_policy={"grant": ["agt-b"]},
        )
    with sqlite3.connect(setup_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM records_v1").fetchone()[0] == 0


def test_unregistered_or_disabled_owner_rejected(setup_db, tmp_path):
    """清单 2 + 裁定 3：未注册/停用 owner 拒写，且不留 running 孤儿批次。"""
    pkg = _write_package(tmp_path, [
        _record("m504-b-001", "灯塔计划合成证据", "2026-07-01T00:00:00Z"),
    ])
    with pytest.raises(ValueError, match="已注册的 agent"):
        import_record_package(pkg, db_path=setup_db, owner_agent_id="agt-ghost")
    _register("agt-x", db_path=setup_db)
    set_agent_enabled("agt-x", False, actor="test", db_path=setup_db)
    with pytest.raises(ValueError, match="启用状态"):
        import_record_package(pkg, db_path=setup_db, owner_agent_id="agt-x")
    # 两次拒绝都发生在登记前/登记事务内回滚：不产生 import_batches 行
    with sqlite3.connect(setup_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM records_v1").fetchone()[0] == 0


def test_same_batch_id_different_input_fails(setup_db, tmp_path):
    """清单 3：同 batch_id 换输入（包体/actor/owner/grant_policy）一律冲突。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    pkg_a = _write_package(tmp_path, [
        _record("m504-c-001", "灯塔计划合成证据甲", "2026-07-01T00:00:00Z"),
    ], name="a.json")
    pkg_b = _write_package(tmp_path, [
        _record("m504-c-002", "灯塔计划合成证据乙", "2026-07-01T00:00:00Z"),
    ], name="b.json")
    import_record_package(
        pkg_a, db_path=setup_db, owner_agent_id="agt-a",
        actor="馆长", batch_id="imp-fixed",
    )
    with pytest.raises(ValueError, match="batch_id 已绑定不同输入"):
        import_record_package(
            pkg_b, db_path=setup_db, owner_agent_id="agt-a",
            actor="馆长", batch_id="imp-fixed",
        )
    with pytest.raises(ValueError, match="actor"):
        import_record_package(
            pkg_a, db_path=setup_db, owner_agent_id="agt-a",
            actor="别人", batch_id="imp-fixed",
        )
    with pytest.raises(ValueError, match="owner_agent_id"):
        import_record_package(
            pkg_a, db_path=setup_db, owner_agent_id="agt-b",
            actor="馆长", batch_id="imp-fixed",
        )
    with pytest.raises(ValueError, match="grant_policy"):
        import_record_package(
            pkg_a, db_path=setup_db, owner_agent_id="agt-a",
            actor="馆长", batch_id="imp-fixed",
            grant_policy={"scope": "shared"},
        )


def test_failed_batch_resume_and_completed_replay(setup_db, tmp_path):
    """清单 13 + 裁定 2：失败批次持久 failed；只能同输入续跑；完成可重放。"""
    _register("agt-a", db_path=setup_db)
    pkg = _write_package(tmp_path, [
        _record(f"m504-d-{i:03d}", f"灯塔计划合成证据 {i}", f"2026-07-0{i}T00:00:00Z")
        for i in range(1, 5)
    ])

    def bomb(summary):
        if summary["added"] >= 2:
            raise RuntimeError("synthetic crash after receipt lost")

    with pytest.raises(RuntimeError, match="receipt lost"):
        import_record_package(
            pkg, db_path=setup_db, owner_agent_id="agt-a",
            actor="馆长", batch_id="imp-crash", batch_size=2,
            progress_callback=bomb,
        )
    assert _batch_status(setup_db, "imp-crash") == "failed"
    # 回执丢失场景：客户端拿同输入重试 → 续跑到完成
    resumed = import_record_package(
        pkg, db_path=setup_db, owner_agent_id="agt-a",
        actor="馆长", batch_id="imp-crash", batch_size=2,
    )
    assert resumed["added"] == 2
    assert resumed["skipped"] == 2
    assert resumed["idempotent_replay"] is False
    assert _batch_status(setup_db, "imp-crash") == "completed"
    # 完成后再重放：直接返回既有 summary，记录一行不动
    replay = import_record_package(
        pkg, db_path=setup_db, owner_agent_id="agt-a",
        actor="馆长", batch_id="imp-crash", batch_size=2,
    )
    assert replay["idempotent_replay"] is True
    assert replay["added"] == 0
    assert replay["skipped"] == 4
    with sqlite3.connect(setup_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM records_v1").fetchone()[0] == 4


# ---------- B. 凭证生命周期（清单 6、9、10） ----------

def test_verify_credential_paths_and_dummy_kdf(setup_db, monkeypatch):
    """清单 6：无效 cred_id 与错误 secret 统一 None；无效 cred_id 走 dummy KDF。"""
    _register("agt-a", db_path=setup_db)
    cred = issue_credential("agt-a", actor="馆长", db_path=setup_db)
    assert verify_credential(cred["token"], db_path=setup_db) == "agt-a"
    # 错误 secret
    assert verify_credential(cred["cred_id"] + ".wrong-secret", db_path=setup_db) is None
    # 畸形 token：不带点 / 空前缀 / 空 secret
    assert verify_credential("no-dot-here", db_path=setup_db) is None
    assert verify_credential(".secret", db_path=setup_db) is None
    assert verify_credential("cred-0123456789abcdef.", db_path=setup_db) is None
    # 无效 cred_id：必须执行 dummy KDF 拉平时间侧信道
    calls = {"dummy": 0}
    real_dummy = identity_module._dummy_kdf

    def counting_dummy():
        calls["dummy"] += 1
        return real_dummy()

    monkeypatch.setattr(identity_module, "_dummy_kdf", counting_dummy)
    assert verify_credential("cred-0123456789abcdef.x", db_path=setup_db) is None
    assert calls["dummy"] == 1


def test_credential_verification_rejects_unapproved_kdf_parameters(setup_db):
    """Stored parameters cannot silently weaken or exhaust the fixed KDF."""
    _register("agt-a", db_path=setup_db)
    cred = issue_credential("agt-a", actor="馆长", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        conn.execute("DROP TRIGGER agent_credentials_immutable_update")
        conn.execute(
            "UPDATE agent_credentials SET params_json = ? WHERE cred_id = ?",
            (json.dumps({"n": 2, "r": 1, "p": 1, "dklen": 16}), cred["cred_id"]),
        )
    assert verify_credential(cred["token"], db_path=setup_db) is None


def test_rotation_grace_and_missing_grace_fails_closed(setup_db):
    """清单 9：轮换后旧凭证在算死的 grace_until 前有效；缺 grace 字段拒绝。"""
    _register("agt-a", db_path=setup_db)
    cred = issue_credential("agt-a", actor="馆长", db_path=setup_db)
    rotated = rotate_credential(cred["cred_id"], actor="馆长", db_path=setup_db)
    assert rotated["previous_cred_id"] == cred["cred_id"]
    assert rotated["token"] != cred["token"]
    assert rotated["grace_until"] is not None
    # 宽限期内旧凭证仍有效，新凭证立即可用
    assert verify_credential(cred["token"], db_path=setup_db) == "agt-a"
    assert verify_credential(rotated["token"], db_path=setup_db) == "agt-a"
    with pytest.raises(IdentityError):
        rotate_credential(cred["cred_id"], actor="馆长", db_path=setup_db)
    # 缺 replacement/grace 的 rotated 事件在数据库边界直接拒绝。
    cred2 = issue_credential("agt-a", actor="馆长", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO credential_events "
                "(cred_id, kind, replacement_cred_id, grace_until, actor, created_at) "
                "VALUES (?, 'rotated', NULL, NULL, 'test', '2026-08-01T00:00:00Z')",
                (cred2["cred_id"],),
            )
    assert verify_credential(cred2["token"], db_path=setup_db) == "agt-a"
    # 不存在的凭证不可轮换
    with pytest.raises(IdentityError):
        rotate_credential("cred-0000000000000000", actor="馆长", db_path=setup_db)


def test_terminal_states_irreversible(setup_db):
    """清单 10：revoked 终态不可逆；停用 agent 不能签发；恢复后才可。"""
    _register("agt-a", db_path=setup_db)
    cred = issue_credential("agt-a", actor="馆长", db_path=setup_db)
    first = revoke_credential(cred["cred_id"], actor="馆长", db_path=setup_db)
    assert first["state"] == "revoked"
    assert first["idempotent_replay"] is False
    assert verify_credential(cred["token"], db_path=setup_db) is None
    # 终态不可轮换
    with pytest.raises(IdentityError):
        rotate_credential(cred["cred_id"], actor="馆长", db_path=setup_db)
    # 重复吊销是幂等重放，状态不二次变化
    second = revoke_credential(cred["cred_id"], actor="馆长", db_path=setup_db)
    assert second["idempotent_replay"] is True
    with sqlite3.connect(setup_db) as conn:
        kinds = [
            r[0] for r in conn.execute(
                "SELECT kind FROM credential_events WHERE cred_id = ? "
                "ORDER BY event_seq", (cred["cred_id"],)
            ).fetchall()
        ]
    assert kinds == ["issued", "revoked"]
    # Even a malformed future writer cannot revive a terminal credential.
    with sqlite3.connect(setup_db) as conn:
        conn.execute(
            "INSERT INTO credential_events "
            "(cred_id, kind, actor, created_at) VALUES (?, 'issued', 'test', ?)",
            (cred["cred_id"], "2099-01-01T00:00:00+00:00"),
        )
    assert verify_credential(cred["token"], db_path=setup_db) is None
    # 停用 agent：拒发；恢复：可发
    set_agent_enabled("agt-a", False, actor="馆长", db_path=setup_db)
    with pytest.raises(IdentityError):
        issue_credential("agt-a", actor="馆长", db_path=setup_db)
    set_agent_enabled("agt-a", True, actor="馆长", db_path=setup_db)
    assert issue_credential("agt-a", actor="馆长", db_path=setup_db)["agent_id"] == "agt-a"


def test_http_credential_auth_matrix(setup_db, monkeypatch):
    """清单 6（HTTP 面）：503/401/403/200 全矩阵；身份只来自 Bearer。"""
    from fastapi.testclient import TestClient
    from backend.trigger.main import app

    monkeypatch.delenv("ACCESS_CODE", raising=False)
    # 无 ACCESS_CODE 且无凭证：fail-closed 503
    with TestClient(app) as client:
        resp = client.post("/api/v1/recall", json={"query": "灯塔"})
        assert resp.status_code == 503
    _register("agt-chen", db_path=setup_db)
    cred = issue_credential("agt-chen", actor="馆长", db_path=setup_db)
    with TestClient(app) as client:
        # 有凭证但缺 Bearer → 401
        resp = client.post("/api/v1/recall", json={"query": "灯塔"})
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"
        # 无效 cred_id 与错误 secret：统一 401，不可区分
        for bad in ("cred-0123456789abcdef.x", f"{cred['cred_id']}.wrong"):
            resp = client.post(
                "/api/v1/recall", json={"query": "灯塔"},
                headers={"Authorization": f"Bearer {bad}"},
            )
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Unauthorized"}
        # 正确凭证 + body 与身份一致 → 200
        resp = client.post(
            "/api/v1/recall", json={"query": "灯塔", "agent_id": "agt-chen"},
            headers={"Authorization": f"Bearer {cred['token']}"},
        )
        assert resp.status_code == 200
        # The compatibility field is optional; Bearer is the identity source.
        resp = client.post(
            "/api/v1/recall", json={"query": "灯塔"},
            headers={"Authorization": f"Bearer {cred['token']}"},
        )
        assert resp.status_code == 200
        # body "default" 别名 agt-legacy，与凭证身份不一致 → 403
        resp = client.post(
            "/api/v1/recall", json={"query": "灯塔", "agent_id": "default"},
            headers={"Authorization": f"Bearer {cred['token']}"},
        )
        assert resp.status_code == 403
        # body 冒名他人 → 403
        resp = client.post(
            "/api/v1/recall", json={"query": "灯塔", "agent_id": "agt-a"},
            headers={"Authorization": f"Bearer {cred['token']}"},
        )
        assert resp.status_code == 403
    # legacy ACCESS_CODE 与凭证并存：访问码仍解析为 agt-legacy
    monkeypatch.setenv("ACCESS_CODE", "synthetic-legacy-code")
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/recall", json={"query": "灯塔"},
            headers={"Authorization": "Bearer synthetic-legacy-code"},
        )
        assert resp.status_code == 200


def test_legacy_recall_body_cannot_select_another_agent(setup_db, monkeypatch):
    """The legacy endpoint authenticates identity before choosing storage scope."""
    from fastapi.testclient import TestClient
    from backend.trigger.main import app

    monkeypatch.setenv("ACCESS_CODE", "synthetic-legacy-code")
    with TestClient(app) as client:
        denied = client.post(
            "/api/recall",
            json={"query": "灯塔", "agent_id": "agt-other"},
            headers={"Authorization": "Bearer synthetic-legacy-code"},
        )
        assert denied.status_code == 403
        allowed = client.post(
            "/api/recall",
            json={"query": "灯塔", "agent_id": "default"},
            headers={"Authorization": "Bearer synthetic-legacy-code"},
        )
        assert allowed.status_code == 200


# ---------- C. 可见性推导（清单 7、8、14；裁定 1、3、4） ----------

def _rowid(db_path, record_id):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT id FROM records_v1 WHERE record_id = ?", (record_id,)
        ).fetchone()[0]


def test_legacy_fallback_visibility(setup_db, tmp_path):
    """清单 8：v5 之前的记录兜底 owner=agt-legacy、scope=private；新 agent 默认不可见。"""
    pkg = _write_package(tmp_path, [
        _record("m504-e-001", "灯塔计划 legacy 合成证据", "2026-07-01T00:00:00Z"),
    ])
    import_record_package(pkg, db_path=setup_db)  # 不带 owner：模拟 v5 前导入
    rowid = _rowid(setup_db, "m504-e-001")
    with sqlite3.connect(setup_db) as conn:
        conn.row_factory = sqlite3.Row
        visibility = current_visibility(conn, rowid)
        assert visibility["owner"] == LEGACY_PRINCIPAL
        assert visibility["epoch"] == 0
        assert visibility["scope"] == "private"
        assert visibility["grants"] == set()
        assert can_read_record(conn, LEGACY_PRINCIPAL, rowid) is True
    _register("agt-new", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        conn.row_factory = sqlite3.Row
        assert can_read_record(conn, "agt-new", rowid) is False
        assert all_visible_rowids(conn, "agt-new") == set()
        # 显式授权后才可见
    grant_access("m504-e-001", "agt-new", actor="馆长", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        conn.row_factory = sqlite3.Row
        assert can_read_record(conn, "agt-new", rowid) is True


def test_scope_shared_then_private_and_grant_interplay(setup_db, tmp_path):
    """清单 14：scope 取最新事件；grant 只对最近 scope_private 之后有效。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    pkg = _write_package(tmp_path, [
        _record("m504-f-001", "灯塔计划 scope 合成证据", "2026-07-01T00:00:00Z"),
    ])
    import_record_package(pkg, db_path=setup_db, owner_agent_id="agt-a")
    rowid = _rowid(setup_db, "m504-f-001")

    def visible_to_b():
        with sqlite3.connect(setup_db) as conn:
            conn.row_factory = sqlite3.Row
            return can_read_record(conn, "agt-b", rowid)

    assert visible_to_b() is False
    set_scope("m504-f-001", "shared", actor="馆长", db_path=setup_db)
    assert visible_to_b() is True
    set_scope("m504-f-001", "private", actor="馆长", db_path=setup_db)
    assert visible_to_b() is False
    grant_access("m504-f-001", "agt-b", actor="馆长", db_path=setup_db)
    assert visible_to_b() is True
    # scope 反复不影响其后授予的 grant
    set_scope("m504-f-001", "shared", actor="馆长", db_path=setup_db)
    assert visible_to_b() is True
    # 再次 private：之前的 grant 晚于旧 private 但早于新 private → 作废
    set_scope("m504-f-001", "private", actor="馆长", db_path=setup_db)
    assert visible_to_b() is False


def test_owner_transfer_new_epoch_voids_grants(setup_db, tmp_path):
    """清单 7：归属转移开新 epoch，旧 owner 的 grant 全部自动失效。"""
    _register("agt-a", "agt-b", "agt-c", db_path=setup_db)
    pkg = _write_package(tmp_path, [
        _record("m504-g-001", "灯塔计划 epoch 合成证据", "2026-07-01T00:00:00Z"),
    ])
    import_record_package(pkg, db_path=setup_db, owner_agent_id="agt-a")
    rowid = _rowid(setup_db, "m504-g-001")
    grant_access("m504-g-001", "agt-b", actor="馆长", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        grant_seq = conn.execute(
            "SELECT event_seq FROM record_visibility_events "
            "WHERE record_rowid = ? AND event_kind = 'grant'", (rowid,)
        ).fetchone()[0]
    set_owner("m504-g-001", "agt-c", actor="馆长", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        conn.row_factory = sqlite3.Row
        visibility = current_visibility(conn, rowid)
        assert visibility["owner"] == "agt-c"
        assert visibility["epoch"] > grant_seq
        assert visibility["grants"] == set()
        assert can_read_record(conn, "agt-c", rowid) is True
        assert can_read_record(conn, "agt-b", rowid) is False
        # 旧 owner 失去身份特权（无 grant、scope private）
        assert can_read_record(conn, "agt-a", rowid) is False
    # 旧 epoch 的 grant 不可再被定向撤销
    with pytest.raises(IdentityError):
        revoke_access("m504-g-001", "agt-b", grant_seq,
                      actor="馆长", db_path=setup_db)


def test_grant_revoke_target_validation(setup_db, tmp_path):
    """清单 7 + 裁定 3：定向撤销在事务内校验同记录/同 agent/当前 epoch/未撤销。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    pkg = _write_package(tmp_path, [
        _record("m504-h-001", "灯塔计划撤销合成证据一", "2026-07-01T00:00:00Z"),
        _record("m504-h-002", "灯塔计划撤销合成证据二", "2026-07-02T00:00:00Z"),
    ])
    import_record_package(pkg, db_path=setup_db, owner_agent_id="agt-a")
    rowid1 = _rowid(setup_db, "m504-h-001")
    grant_access("m504-h-001", "agt-b", actor="馆长", db_path=setup_db)
    grant_access("m504-h-002", "agt-b", actor="馆长", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        g1 = conn.execute(
            "SELECT event_seq FROM record_visibility_events "
            "WHERE record_rowid = ? AND event_kind = 'grant'", (rowid1,)
        ).fetchone()[0]
        g2 = conn.execute(
            "SELECT event_seq FROM record_visibility_events "
            "WHERE record_rowid = ? AND event_kind = 'grant'",
            (_rowid(setup_db, "m504-h-002"),),
        ).fetchone()[0]
    # 跨记录的 grant seq → 拒绝
    with pytest.raises(IdentityError):
        revoke_access("m504-h-001", "agt-b", g2, actor="馆长", db_path=setup_db)
    # 同记录但 agent 不匹配 → 拒绝
    with pytest.raises(IdentityError):
        revoke_access("m504-h-001", "agt-a", g1, actor="馆长", db_path=setup_db)
    # 非整数 seq → 拒绝
    with pytest.raises(IdentityError):
        revoke_access("m504-h-001", "agt-b", "g1", actor="馆长", db_path=setup_db)
    with pytest.raises(IdentityError):
        revoke_access("m504-h-001", "agt-b", True, actor="馆长", db_path=setup_db)
    # 合法撤销生效
    result = revoke_access("m504-h-001", "agt-b", g1, actor="馆长", db_path=setup_db)
    assert result["revoked"] == "agt-b"
    with sqlite3.connect(setup_db) as conn:
        conn.row_factory = sqlite3.Row
        assert can_read_record(conn, "agt-b", rowid1) is False
    # 重复撤销同一目标 → 拒绝
    with pytest.raises(IdentityError):
        revoke_access("m504-h-001", "agt-b", g1, actor="馆长", db_path=setup_db)
    # 重新授权（新事件）后恢复可见；重复 grant 幂等
    first = grant_access("m504-h-001", "agt-b", actor="馆长", db_path=setup_db)
    assert first["idempotent_replay"] is False
    again = grant_access("m504-h-001", "agt-b", actor="馆长", db_path=setup_db)
    assert again["idempotent_replay"] is True
    with sqlite3.connect(setup_db) as conn:
        conn.row_factory = sqlite3.Row
        assert can_read_record(conn, "agt-b", rowid1) is True


def test_grant_before_latest_private_cannot_be_revoked(setup_db, tmp_path):
    """已经被新 private epoch 作废的旧授权，不再伪装成当前有效授权。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    pkg = _write_package(tmp_path, [
        _record("m504-h-private", "灯塔计划过期授权", "2026-07-01T00:00:00Z"),
    ])
    import_record_package(pkg, db_path=setup_db, owner_agent_id="agt-a")
    rowid = _rowid(setup_db, "m504-h-private")
    grant_access("m504-h-private", "agt-b", actor="馆长", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        grant_seq = conn.execute(
            "SELECT event_seq FROM record_visibility_events "
            "WHERE record_rowid = ? AND event_kind = 'grant'",
            (rowid,),
        ).fetchone()[0]
    set_scope("m504-h-private", "private", actor="馆长", db_path=setup_db)
    with pytest.raises(IdentityError):
        revoke_access(
            "m504-h-private",
            "agt-b",
            grant_seq,
            actor="馆长",
            db_path=setup_db,
        )


def test_actor_is_audit_attribution_only(setup_db, tmp_path):
    """裁定 1：actor 原样留痕，但不是身份、不产生任何权限。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    pkg = _write_package(tmp_path, [
        _record("m504-i-001", "灯塔计划归因合成证据", "2026-07-01T00:00:00Z"),
    ])
    import_record_package(pkg, db_path=setup_db, owner_agent_id="agt-a")
    set_owner("m504-i-001", "agt-b", actor="沈先生", db_path=setup_db)
    with sqlite3.connect(setup_db) as conn:
        actors = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT actor FROM record_visibility_events"
            ).fetchall()
        ]
    assert "沈先生" in actors
    # 未注册字符串不能成为管理对象，也不能被授权
    with pytest.raises(IdentityError):
        issue_credential("沈先生", actor="馆长", db_path=setup_db)
    with pytest.raises(IdentityError):
        grant_access("m504-i-001", "沈先生", actor="馆长", db_path=setup_db)


def test_config_kv_and_event_tables_immutable(setup_db, tmp_path):
    """裁定 4：config_kv 锚点不可改；事件/凭证表 UPDATE/DELETE 被触发器拒绝。"""
    _register("agt-a", db_path=setup_db)
    cred = issue_credential("agt-a", actor="馆长", db_path=setup_db)
    pkg = _write_package(tmp_path, [
        _record("m504-j-001", "灯塔计划锚点合成证据", "2026-07-01T00:00:00Z"),
    ])
    import_record_package(pkg, db_path=setup_db, owner_agent_id="agt-a")

    with sqlite3.connect(setup_db) as conn:
        assert conn.execute(
            "SELECT value FROM config_kv WHERE key = 'legacy_principal'"
        ).fetchone()[0] == LEGACY_PRINCIPAL
    forbidden = [
        ("UPDATE config_kv SET value = 'x' WHERE key = 'legacy_principal'", ()),
        ("DELETE FROM config_kv WHERE key = 'legacy_principal'", ()),
        ("UPDATE agent_events SET kind = 'x'", ()),
        ("DELETE FROM agent_events", ()),
        ("UPDATE agent_credentials SET agent_id = 'x' WHERE cred_id = ?",
         (cred["cred_id"],)),
        ("DELETE FROM agent_credentials WHERE cred_id = ?", (cred["cred_id"],)),
        ("UPDATE credential_events SET kind = 'x'", ()),
        ("DELETE FROM credential_events", ()),
        ("UPDATE record_visibility_events SET target_agent = 'x'", ()),
        ("DELETE FROM record_visibility_events", ()),
    ]
    for sql, params in forbidden:
        with sqlite3.connect(setup_db) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, params)
    # 迁移可重复执行：锚点不重复插入
    first_counts = _event_counts(setup_db)
    assert migrate_records_db(setup_db)["applied"] == []
    assert _event_counts(setup_db) == first_counts


def _event_counts(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "agents", "agent_events", "agent_credentials",
                "credential_events", "record_visibility_events", "config_kv",
            )
        }


# ---------- D. 召回与投影（清单 4、5、11、12；裁定 5） ----------

def _import_owned(setup_db, tmp_path, specs, owner):
    pkg = _write_package(tmp_path, [
        _record(rid, content, created_at) for rid, content, created_at in specs
    ], name=f"pkg-{owner}-{len(specs)}.json")
    import_record_package(pkg, db_path=setup_db, owner_agent_id=owner)
    return [rid for rid, _, _ in specs]


def test_acl_applied_before_limit(setup_db, tmp_path):
    """清单 4：可见性谓词先于排序与 LIMIT；post-filter 会丢的结果必须召回。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    strong = "灯塔计划 " * 12
    specs = [
        ("m504-k-001", "灯塔计划。", "2026-07-01T00:00:00Z"),  # 最旧、词频最低
        ("m504-k-002", strong + "补充二甲", "2026-07-02T00:00:00Z"),
        ("m504-k-003", strong + "补充三甲", "2026-07-03T00:00:00Z"),
        ("m504-k-004", strong + "补充四甲", "2026-07-04T00:00:00Z"),
    ]
    _import_owned(setup_db, tmp_path, specs, "agt-a")
    # agt-b 只被授予排序最靠后的那一条
    grant_access("m504-k-001", "agt-b", actor="馆长", db_path=setup_db)
    result = recall_records("灯塔计划", limit=1, db_path=setup_db, agent_id="agt-b")
    assert [m["record_id"] for m in result["memories"]] == ["m504-k-001"]
    # owner 视角不受授权影响，limit=2 取排名前二
    owner_view = recall_records(
        "灯塔计划", limit=2, db_path=setup_db, agent_id="agt-a"
    )
    assert len(owner_view["memories"]) == 2
    assert "m504-k-001" not in [m["record_id"] for m in owner_view["memories"]]


def test_coverage_matches_visible_set(setup_db, tmp_path):
    """清单 12：coverage 与正文同一份可见集合；空可见集 coverage 全 None。"""
    _register("agt-a", "agt-b", "agt-c", db_path=setup_db)
    _import_owned(setup_db, tmp_path, [
        ("m504-l-001", "灯塔计划 coverage 旧证据", "2026-07-01T00:00:00Z"),
        ("m504-l-002", "灯塔计划 coverage 新证据", "2026-07-05T00:00:00Z"),
    ], "agt-a")
    grant_access("m504-l-001", "agt-b", actor="馆长", db_path=setup_db)
    as_b = recall_records("灯塔计划", db_path=setup_db, agent_id="agt-b")
    assert as_b["coverage"]["latest_imported_record_at"] == "2026-07-01T00:00:00Z"
    assert as_b["coverage"]["verified_knowledge_cutoff_at"] == "2026-08-01T00:00:00Z"
    as_a = recall_records("灯塔计划", db_path=setup_db, agent_id="agt-a")
    assert as_a["coverage"]["latest_imported_record_at"] == "2026-07-05T00:00:00Z"
    # 无授权 agent：正文空、coverage 全 None、不产生 500
    as_c = recall_records("灯塔计划", db_path=setup_db, agent_id="agt-c")
    assert as_c["memories"] == []
    assert as_c["coverage"]["latest_imported_record_at"] is None
    assert as_c["coverage"]["verified_knowledge_cutoff_at"] is None
    assert as_c["coverage"]["coverage_status"] == "no_visible_records"


def test_projected_recall_restricted_placeholder(setup_db, tmp_path):
    """清单 11：restricted 占位符全面脱敏，绝不含 freshness/stale/content。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    _import_owned(setup_db, tmp_path, [
        ("m504-m-001", "灯塔计划 restricted 证据一", "2026-07-01T00:00:00Z"),
        ("m504-m-002", "灯塔计划 restricted 证据二", "2026-07-02T00:00:00Z"),
    ], "agt-a")
    build_projection(
        [{"claim_key": "m504:r", "claim_kind": "fact",
          "content": "合成 claim：双证据",
          "evidence_record_ids": ["m504-m-001", "m504-m-002"]}],
        agent_id="agt-a", rule_id="synthetic-rule", db_path=setup_db,
    )
    # 证据二易主 → agt-a 对 claim 的跨页证据失去可见性
    set_owner("m504-m-002", "agt-b", actor="馆长", db_path=setup_db)
    result = recall_with_projection("灯塔计划", agent_id="agt-a", db_path=setup_db)
    assert [m["record_id"] for m in result["memories"]] == ["m504-m-001"]
    memory = result["memories"][0]
    assert memory["projection_status"] == "restricted"
    assert len(memory["claims"]) == 1
    claim = memory["claims"][0]
    assert set(claim.keys()) == {
        "claim_id", "claim_version", "restricted", "restricted_reason",
    }
    assert claim["restricted"] is True
    assert claim["restricted_reason"] == "evidence_not_visible"


def test_conflict_group_masked_when_member_evidence_invisible(setup_db, tmp_path):
    """清单 5：冲突组任一成员证据不可见 → 整组只剩 {conflict_id, restricted}。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    _import_owned(setup_db, tmp_path, [
        ("m504-n-001", "灯塔计划 冲突证据甲", "2026-07-01T00:00:00Z"),
        ("m504-n-002", "灯塔计划 冲突证据乙", "2026-07-02T00:00:00Z"),
    ], "agt-a")
    built = build_projection(
        [
            {"claim_key": "m504:c1", "claim_kind": "fact",
             "content": "合成 claim 甲", "evidence_record_ids": ["m504-n-001"]},
            {"claim_key": "m504:c2", "claim_kind": "fact",
             "content": "合成 claim 乙", "evidence_record_ids": ["m504-n-002"]},
        ],
        agent_id="agt-a", rule_id="synthetic-rule", db_path=setup_db,
    )
    claim_ids = [c["claim_id"] for c in built["claims"]]
    conflict_id = register_conflict(
        "m504:topic", [{"claim_id": cid} for cid in claim_ids],
        agent_id="agt-a", db_path=setup_db,
    )["conflict_id"]
    # 转移前：完整呈现（正面对照）
    before = recall_with_projection("灯塔计划", agent_id="agt-a", db_path=setup_db)
    before_claims = {
        m["record_id"]: m["claims"] for m in before["memories"] if m["claims"]
    }
    full = before_claims["m504-n-001"][0]["conflicts"][0]
    assert full["conflict_id"] == conflict_id
    assert full["status"] == "open"
    assert "topic_key" in full and "presentation" in full
    # 成员乙的证据易主 → 整组脱敏
    set_owner("m504-n-002", "agt-b", actor="馆长", db_path=setup_db)
    after = recall_with_projection("灯塔计划", agent_id="agt-a", db_path=setup_db)
    assert [m["record_id"] for m in after["memories"]] == ["m504-n-001"]
    masked = after["memories"][0]["claims"][0]["conflicts"][0]
    assert masked == {"conflict_id": conflict_id, "status": "restricted"}


def test_projection_build_gate_blocks_invisible_evidence(setup_db, tmp_path):
    """裁定 5：投影构建是 DB 写；不可见证据直接拒建，授权后放行。"""
    _register("agt-a", "agt-b", db_path=setup_db)
    _import_owned(setup_db, tmp_path, [
        ("m504-o-001", "灯塔计划 闸门证据", "2026-07-01T00:00:00Z"),
    ], "agt-a")
    items = [
        {"claim_key": "m504:gate", "claim_kind": "fact",
         "content": "合成 claim：越权构建", "evidence_record_ids": ["m504-o-001"]},
    ]
    with pytest.raises(ValueError, match="不可见"):
        build_projection(
            items, agent_id="agt-b", rule_id="r1", db_path=setup_db,
        )
    grant_access("m504-o-001", "agt-b", actor="馆长", db_path=setup_db)
    result = build_projection(
        items, agent_id="agt-b", rule_id="r1", db_path=setup_db,
    )
    assert result["created"] == 1


def test_direct_claim_and_conflict_reads_follow_evidence_visibility(
    setup_db, tmp_path
):
    """Local read helpers must not become a future ACL bypass."""
    from backend.memory.claim_conflicts import get_conflict, list_conflicts
    from backend.memory.projection import claim_provenance, get_claim, list_claims

    _register("agt-a", "agt-b", db_path=setup_db)
    _import_owned(setup_db, tmp_path, [
        ("m504-direct-001", "灯塔计划直接读取甲", "2026-07-01T00:00:00Z"),
        ("m504-direct-002", "灯塔计划直接读取乙", "2026-07-02T00:00:00Z"),
    ], "agt-a")
    built = build_projection(
        [
            {"claim_key": "m504:direct-1", "claim_kind": "fact",
             "content": "合成直接 Claim 甲",
             "evidence_record_ids": ["m504-direct-001"]},
            {"claim_key": "m504:direct-2", "claim_kind": "fact",
             "content": "合成直接 Claim 乙",
             "evidence_record_ids": ["m504-direct-002"]},
        ],
        agent_id="agt-a", rule_id="direct-read", db_path=setup_db,
    )
    claim_ids = [item["claim_id"] for item in built["claims"]]
    conflict_id = register_conflict(
        "m504:direct-topic", [{"claim_id": cid} for cid in claim_ids],
        agent_id="agt-a", db_path=setup_db,
    )["conflict_id"]
    assert get_claim(claim_ids[1], "agt-a", db_path=setup_db) is not None
    assert claim_provenance(claim_ids[1], "agt-a", db_path=setup_db) is not None
    assert len(list_claims("agt-a", db_path=setup_db)) == 2
    assert get_conflict(conflict_id, "agt-a", setup_db)["members"]

    set_owner("m504-direct-002", "agt-b", actor="馆长", db_path=setup_db)
    assert get_claim(claim_ids[1], "agt-a", db_path=setup_db) is None
    assert claim_provenance(claim_ids[1], "agt-a", db_path=setup_db) is None
    assert [row["claim_id"] for row in list_claims("agt-a", db_path=setup_db)] == [
        claim_ids[0]
    ]
    masked = get_conflict(conflict_id, "agt-a", setup_db)
    assert masked == {
        "conflict": {"conflict_id": conflict_id, "status": "restricted"},
        "members": [],
        "decisions": [],
    }
    assert list_conflicts("agt-a", db_path=setup_db) == []


def test_static_scan_invariants(setup_db):
    """清单 15：关键不变量静态扫描（不依赖运行时偶发行为）。"""
    import inspect
    import re

    import backend.memory.records_v1 as records_module
    import backend.trigger.auth as auth_module
    import backend.trigger.routes as routes_module

    identity_src = inspect.getsource(identity_module)
    records_src = inspect.getsource(records_module)
    routes_src = inspect.getsource(routes_module)
    auth_src = inspect.getsource(auth_module)

    # 不存在 shared_all / argon2 这类被终审否决的设计残留
    for src in (identity_src, records_src, routes_src, auth_src):
        assert "shared_all" not in src
        assert "argon2" not in src.lower()
    assert "scrypt" in identity_src

    # v5 迁移：五张表的 UPDATE/DELETE 不可变触发器 + legacy 锚点
    migration_sql = "\n".join(records_module.MIGRATION_5_STATEMENTS)
    for table in (
        "agents", "agent_events", "agent_credentials", "credential_events",
        "record_visibility_events", "config_kv",
    ):
        assert re.search(rf"BEFORE UPDATE ON {table}\b", migration_sql)
        assert re.search(rf"BEFORE DELETE ON {table}\b", migration_sql)
    assert "legacy_principal" in migration_sql
    assert LEGACY_PRINCIPAL in migration_sql
    assert records_module.MIGRATION_VERSION == 5

    # 召回可见性谓词：两处 SQL 都先于 ORDER BY / LIMIT（fail-closed 空集也在）
    recall_src = inspect.getsource(records_module.recall_records)
    vis_marks = [m.start() for m in re.finditer(r"\{vis_where\}", recall_src)]
    assert len(vis_marks) == 2
    assert vis_marks[0] < recall_src.index("ORDER BY lexical_rank")
    assert vis_marks[1] < recall_src.index("ORDER BY r.created_at DESC")
    assert "AND 0" in recall_src

    # 路由面：无网络投影构建写入口、无网络管理写入口；所有召回共用身份解析
    assert "build_projection" not in routes_src
    assert "/v1/projection" not in routes_src
    for admin_verb in (
        "register_agent", "issue_credential", "rotate_credential",
        "revoke_credential", "set_owner", "grant_access",
    ):
        assert admin_verb not in routes_src
    assert "resolve_principal" in routes_src
    assert "require_access_code" not in routes_src
    assert "def require_access_code" not in auth_src
    assert "verify_credential" in auth_src
    assert "compare_digest" in auth_src
