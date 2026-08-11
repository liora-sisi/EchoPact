"""M4.5 第二批：agent 归属隔离测试。

覆盖工单：
- 错误 agent 不得读取、增加 recall_count、完成或删除其他 agent 的 Memory；
- 错误 agent 不得读取或完成其他 agent 的 Saga，且报错不泄露归属；
- 主动候选、动态间隔、状态推断、冷却键按 agent 隔离；
- active_push 脚本同时发送 agent_id 与 Bearer，未配置访问码时 fail-closed；
- 默认 agent 兼容；
- 全程不联网（脚本测试 monkeypatch requests.post）。
"""
import importlib.util
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

import backend.utils.db as db_module
from backend.utils.db import init_db, get_conn
from backend.memory.models import Memory
from backend.memory.crud import (
    create_memory, get_memory, list_undone, mark_done, delete_memory,
)
from backend.memory.saga import create_saga, get_saga, complete_saga
from backend.judge.active_recall import (
    _ensure_meta_table, _get_last_push_time, _update_last_push_time,
    pick_memory_to_push, should_push, get_dynamic_interval,
)
from backend.memory.context import update_last_action, infer_user_state
from backend.memory.profile import update_conscientiousness


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    db_module.DB_PATH = str(tmp_path / "iso_test.db")
    init_db()
    _ensure_meta_table()


def _mk(agent: str, content: str, is_done: bool = False) -> int:
    return create_memory(Memory(content=content, agent_id=agent, is_done=is_done))


def _raw_row(mid: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()


# ---------- Memory 隔离 ----------

def test_get_memory_cross_agent_denied_and_no_count():
    mid = _mk("agent-b", "B 的私密记忆")
    assert get_memory(mid, agent_id="agent-a") is None
    assert get_memory(mid, agent_id="agent-a") is None
    # 两次跨 agent 读取都不得增加 recall_count
    assert _raw_row(mid)["recall_count"] == 0
    # 属主正常读取
    assert get_memory(mid, agent_id="agent-b") is not None


def test_list_undone_scoped_by_agent():
    _mk("agent-a", "A 的待办")
    _mk("agent-b", "B 的待办")
    a_items = list_undone(agent_id="agent-a")
    assert len(a_items) == 1 and a_items[0].content == "A 的待办"


def test_mark_done_cross_agent_denied():
    mid = _mk("agent-b", "B 的事")
    mark_done(mid, agent_id="agent-a")
    assert _raw_row(mid)["is_done"] == 0
    mark_done(mid, agent_id="agent-b")
    assert _raw_row(mid)["is_done"] == 1


def test_delete_memory_cross_agent_denied():
    mid = _mk("agent-b", "B 的记录")
    delete_memory(mid, agent_id="agent-a")
    assert _raw_row(mid) is not None
    delete_memory(mid, agent_id="agent-b")
    assert _raw_row(mid) is None


# ---------- Saga 隔离 ----------

def test_get_saga_cross_agent_denied():
    sid = create_saga("B 的主线", agent_id="agent-b")
    assert get_saga(sid, agent_id="agent-a") is None
    assert get_saga(sid, agent_id="agent-b") is not None


def test_complete_saga_cross_agent_denied_non_revealing():
    sid = create_saga("B 的主线", agent_id="agent-b")
    with pytest.raises(ValueError) as exc_info:
        complete_saga(sid, agent_id="agent-a")
    # 报错不得泄露记录属于哪个其他 agent
    assert "agent-b" not in str(exc_info.value)
    assert get_saga(sid, agent_id="agent-b")["status"] == "active"


def test_complete_saga_missing_same_wording_as_cross_agent():
    sid = create_saga("B 的主线", agent_id="agent-b")
    cross = None
    missing = None
    try:
        complete_saga(sid, agent_id="agent-a")
    except ValueError as e:
        cross = str(e)
    try:
        complete_saga(999999, agent_id="agent-a")
    except ValueError as e:
        missing = str(e)
    assert cross is not None and cross == missing


# ---------- 主动召回全链隔离 ----------

def test_pick_memory_scoped_by_agent():
    _mk("agent-b", "只有 B 有记忆")
    assert pick_memory_to_push(agent_id="agent-a") is None
    best = pick_memory_to_push(agent_id="agent-b")
    assert best is not None and "B" in best["content"]


def test_cooldown_key_per_agent():
    _update_last_push_time("agent-a")
    assert _get_last_push_time("agent-a") is not None
    assert _get_last_push_time("agent-b") is None
    with get_conn() as conn:
        keys = {r["key"] for r in conn.execute("SELECT key FROM system_meta").fetchall()}
    assert "last_active_push:agent-a" in keys


def test_should_push_cooldown_isolated_per_agent():
    old_msg = datetime.now(timezone.utc) - timedelta(minutes=60)
    _update_last_push_time("agent-a")  # A 刚推过，冷却中
    assert should_push(old_msg, "agent-a") is False
    assert should_push(old_msg, "agent-b") is True


def test_dynamic_interval_per_agent():
    update_conscientiousness(0.4, "agent-diligent")  # 0.9 → 120 分钟
    assert get_dynamic_interval("agent-diligent") == 120
    assert get_dynamic_interval("agent-plain") == 60


def test_infer_user_state_per_agent():
    update_last_action("我在吃饺子", agent_id="agent-a")
    update_last_action("我在睡觉", agent_id="agent-b")
    state_a = infer_user_state("agent-a")
    state_b = infer_user_state("agent-b")
    assert state_a != state_b
    assert infer_user_state("agent-c") is None


# ---------- 默认 agent 兼容 ----------

def test_default_agent_backward_compatible():
    mid = _mk("default", "默认记忆")
    assert get_memory(mid) is not None
    mark_done(mid)
    assert get_memory(mid).is_done is True
    sid = create_saga("默认主线")
    complete_saga(sid)
    assert get_saga(sid)["status"] == "completed"


# ---------- active_push 脚本（不联网） ----------

def _load_active_push(monkeypatch, access_code: str = "", agent_id: str = "agent-x"):
    monkeypatch.setenv("ACCESS_CODE", access_code)
    monkeypatch.setenv("AGENT_ID", agent_id)
    spec = importlib.util.spec_from_file_location(
        "active_push_under_test",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "active_push.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_active_push_sends_agent_id_and_bearer(monkeypatch):
    module = _load_active_push(monkeypatch, access_code="script-test-code-9z")
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"memories": [{"content": "x", "weight": 0.1}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResp()

    monkeypatch.setattr(module.requests, "post", fake_post)
    module.get_memory_to_push("script-test-code-9z", "agent-x")
    assert captured["json"]["agent_id"] == "agent-x"
    assert captured["headers"]["Authorization"] == "Bearer script-test-code-9z"


def test_active_push_fail_closed_without_access_code(monkeypatch):
    module = _load_active_push(monkeypatch, access_code="")
    called = []
    monkeypatch.setattr(
        module.requests, "post",
        lambda *a, **k: called.append(1),
    )
    with pytest.raises(SystemExit):
        module.main()
    assert called == []  # 未配置访问码时不得发出任何网络请求
