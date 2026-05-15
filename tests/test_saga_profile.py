import pytest
import backend.utils.db as db_module
from backend.utils.db import init_db
from backend.memory.saga import create_saga, complete_saga, list_sagas, get_saga
from backend.memory.profile import (
    get_profile, master_written, master_delayed,
    get_push_interval, DEFAULT_CONSCIENTIOUSNESS
)

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_module.DB_PATH = str(db_file)
    init_db()

def test_create_saga():
    sid = create_saga("拖master", agent_id="baozhang")
    saga = get_saga(sid)
    assert saga["title"] == "拖master"
    assert saga["status"] == "active"

def test_complete_saga():
    sid = create_saga("写完master", agent_id="baozhang")
    complete_saga(sid)
    saga = get_saga(sid)
    assert saga["status"] == "completed"

def test_list_active_sagas():
    create_saga("saga1", agent_id="test")
    create_saga("saga2", agent_id="test")
    active = list_sagas(agent_id="test", status="active")
    assert len(active) == 2

def test_default_conscientiousness():
    profile = get_profile(agent_id="new_agent")
    assert profile["conscientiousness"] == DEFAULT_CONSCIENTIOUSNESS

def test_master_written():
    score = master_written(agent_id="test")
    assert score > DEFAULT_CONSCIENTIOUSNESS

def test_master_delayed():
    score = master_delayed(agent_id="test")
    assert score < DEFAULT_CONSCIENTIOUSNESS

def test_conscientiousness_cap():
    for _ in range(100):
        master_written(agent_id="test")
    profile = get_profile(agent_id="test")
    assert profile["conscientiousness"] <= 1.0

def test_push_interval_low():
    for _ in range(30):
        master_delayed(agent_id="lazy")
    interval = get_push_interval(agent_id="lazy")
    assert interval == 15

def test_push_interval_high():
    for _ in range(50):
        master_written(agent_id="hardworking")
    interval = get_push_interval(agent_id="hardworking")
    assert interval == 120
