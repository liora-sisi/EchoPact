import pytest
import os
from backend.memory.embeddings import embed_one, embed
from backend.memory.vector_store import upsert_memory, search_similar, get_collection

@pytest.fixture(autouse=True)
def clean_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))

def test_embed_one():
    vec = embed_one("保长嘴臭但靠谱")
    assert isinstance(vec, list)
    assert len(vec) > 0

def test_embed_batch():
    vecs = embed(["烧烤真好吃", "master还没写"])
    assert len(vecs) == 2

def test_upsert_and_search():
    upsert_memory(1, "保长催我写master", agent_id="test")
    upsert_memory(2, "烧烤好吃羊肉香", agent_id="test")
    upsert_memory(3, "贝贝猪在睡觉", agent_id="test")
    results = search_similar("催更", limit=2, agent_id="test")
    assert len(results) > 0
    assert results[0]["id"] == 1

def test_agent_isolation():
    upsert_memory(1, "克里的记忆", agent_id="keli")
    upsert_memory(1, "保长的记忆", agent_id="baozhang")
    keli_results = search_similar("记忆", limit=5, agent_id="keli")
    bao_results = search_similar("记忆", limit=5, agent_id="baozhang")
    assert keli_results[0]["content"] == "克里的记忆"
    assert bao_results[0]["content"] == "保长的记忆"
