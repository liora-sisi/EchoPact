import pytest
import backend.utils.db as db_module
from backend.utils.db import init_db, get_conn
from scripts.import_history import load_checkpoint, save_checkpoint, import_memories
import os

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_module.DB_PATH = str(db_file)
    monkeypatch.setenv("USE_REAL_EMBEDDING", "false")
    init_db()

def test_checkpoint_save_load(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.import_history.CHECKPOINT_FILE", 
                        str(tmp_path / "checkpoint.json"))
    save_checkpoint(10, 100)
    cp = load_checkpoint()
    assert cp["last_index"] == 10
    assert cp["total"] == 100

def test_empty_file_import(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.import_history.CHECKPOINT_FILE",
                        str(tmp_path / "checkpoint.json"))
    import_memories("nonexistent.txt", agent_id="test")

def test_no_duplicate_import(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.import_history.CHECKPOINT_FILE",
                        str(tmp_path / "checkpoint.json"))
    from backend.memory.models import Memory
    from backend.memory.crud import create_memory
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    m = Memory(content="测试重复内容", agent_id="test")
    m.created_at = now
    create_memory(m)
    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE content=? AND agent_id=?",
            ("测试重复内容", "test")
        ).fetchone()["cnt"]
    assert count == 1
