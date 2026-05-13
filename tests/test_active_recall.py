import pytest
from datetime import datetime, timezone, timedelta
from backend.judge.active_recall import (
    _active_weight,
    should_push,
    pick_memory_to_push,
    _ensure_meta_table,
)
from backend.utils.db import init_db
from backend.memory.models import Memory
from backend.memory.crud import create_memory
import backend.utils.db as db_module

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_module.DB_PATH = str(db_file)
    init_db()
    _ensure_meta_table()

def test_active_weight_undone():
    score = _active_weight(0.0, 0.0, is_done=0, recall_count=0)
    assert score == pytest.approx(0.7, abs=0.01)

def test_active_weight_done():
    score = _active_weight(0.9, 0.9, is_done=1, recall_count=0)
    assert score < 0.5

def test_should_push_silence():
    old_time = datetime.now(timezone.utc) - timedelta(minutes=50)
    assert should_push(old_time) == True

def test_should_push_too_soon():
    recent_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert should_push(recent_time) == False

def test_pick_memory():
    mem = Memory(
        content="master还没写",
        valence=0.5, arousal=0.8,
        is_done=False
    )
    create_memory(mem)
    result = pick_memory_to_push()
    assert result is not None
    assert "master" in result["content"]
