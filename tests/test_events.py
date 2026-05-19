import pytest
import backend.utils.db as db_module
from backend.utils.db import init_db
from backend.memory.events import log_event, get_events

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_module.DB_PATH = str(db_file)
    init_db()

def test_log_event():
    eid = log_event("牛牛说贝贝猪今天吃了虾", agent_id="test")
    assert eid > 0

def test_get_events():
    log_event("保长催我写master", agent_id="test")
    log_event("克里帮我写代码", agent_id="test")
    events = get_events(agent_id="test")
    assert len(events) == 2

def test_append_only():
    """事件只能插入，验证没有update/delete方法"""
    from backend.memory import events
    assert not hasattr(events, 'delete_event')
    assert not hasattr(events, 'update_event')

def test_agent_isolation():
    log_event("保长的事件", agent_id="baozhang")
    log_event("克里的事件", agent_id="keli")
    bao_events = get_events(agent_id="baozhang")
    keli_events = get_events(agent_id="keli")
    assert len(bao_events) == 1
    assert len(keli_events) == 1
