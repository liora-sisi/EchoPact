import pytest
import backend.utils.db as db_module
from backend.utils.db import init_db
from backend.memory.models import Memory
from backend.memory.crud import create_memory
from backend.judge.recall import recall_memories
from unittest.mock import patch

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_module.DB_PATH = str(db_file)
    init_db()
    monkeypatch.setenv("USE_REAL_EMBEDDING", "false")

@pytest.fixture(autouse=True)
def mock_vector(monkeypatch):
    monkeypatch.setattr(
        "backend.memory.vector_store.search_similar",
        lambda query, limit, agent_id: []
    )

def test_undone_bonus():
    m1 = Memory(content="未完成的事", is_done=False, agent_id="test")
    m2 = Memory(content="已完成的事", is_done=True, agent_id="test")
    create_memory(m1)
    create_memory(m2)
    results = recall_memories("事情", limit=2, agent_id="test")
    assert results[0]["content"] == "未完成的事"

def test_saga_boost():
    from backend.utils.db import get_conn
    m1 = Memory(content="saga记忆", agent_id="test")
    m2 = Memory(content="普通记忆", agent_id="test")
    id1 = create_memory(m1)
    create_memory(m2)
    with get_conn() as conn:
        conn.execute("UPDATE memories SET saga_id=1 WHERE id=?", (id1,))
    results = recall_memories("记忆", limit=2, agent_id="test")
    assert results[0]["content"] == "saga记忆"

def test_reason_fields():
    m = Memory(content="测试记忆", agent_id="test")
    create_memory(m)
    results = recall_memories("测试", limit=1, agent_id="test")
    assert "reason" in results[0]
    assert "sim" in results[0]["reason"]
    assert "emotion_fit" in results[0]["reason"]
    assert "decay" in results[0]["reason"]
    assert "saga_boost" in results[0]["reason"]

def test_agent_isolation():
    m1 = Memory(content="克里的记忆", agent_id="keli")
    m2 = Memory(content="保长的记忆", agent_id="baozhang")
    create_memory(m1)
    create_memory(m2)
    keli = recall_memories("记忆", agent_id="keli")
    bao = recall_memories("记忆", agent_id="baozhang")
    assert all(r["content"] == "克里的记忆" for r in keli)
    assert all(r["content"] == "保长的记忆" for r in bao)
