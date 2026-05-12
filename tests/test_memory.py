import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.utils.db import init_db
from backend.memory.models import Memory
from backend.memory.crud import (
    create_memory,
    get_memory,
    list_memories,
    list_undone,
    mark_done,
    delete_memory,
    estimate_importance,
)

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import backend.utils.db as db_module
    db_module.DB_PATH = str(db_file)
    init_db()

def test_create_and_get():
    mem = Memory(content="牛牛今天买了水晶", valence=0.8, arousal=0.5, direction="self")
    mid = create_memory(mem)
    result = get_memory(mid)
    assert result.content == "牛牛今天买了水晶"
    assert result.valence == 0.8

def test_emotion_weight():
    mem = Memory(content="保长嘴臭", valence=-0.8, arousal=0.9, direction="other")
    assert abs(mem.emotion_weight() - 0.72) < 0.01

def test_importance_boost_keyword():
    score = estimate_importance("保长又在催我写master")
    assert score >= 0.7

def test_importance_emotion_keyword():
    score = estimate_importance("我哭了好难过")
    assert score >= 0.8

def test_importance_cap():
    score = estimate_importance("保长催我哭了好难过master")
    assert score == 1.0

def test_recall_count_increment():
    mem = Memory(content="测试recall计数")
    mid = create_memory(mem)
    get_memory(mid)
    get_memory(mid)
    result = get_memory(mid)
    assert result.recall_count == 3

def test_list_undone():
    mem = Memory(content="换端口", valence=0.0, arousal=0.3, direction="event", is_done=False)
    create_memory(mem)
    undone = list_undone()
    assert any(m.content == "换端口" for m in undone)

def test_mark_done():
    mem = Memory(content="写master", is_done=False)
    mid = create_memory(mem)
    mark_done(mid)
    result = get_memory(mid)
    assert result.is_done == True

def test_delete():
    mem = Memory(content="要被删的记忆")
    mid = create_memory(mem)
    delete_memory(mid)
    assert get_memory(mid) is None
    
