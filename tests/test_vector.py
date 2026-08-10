import pytest

from backend.memory import embeddings, vector_store

@pytest.fixture(autouse=True)
def mock_embed(monkeypatch):
    def fake_embed(texts):
        return [[0.1] * 1536 for _ in texts]

    def fake_embed_one(text):
        return [0.1] * 1536

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    monkeypatch.setattr(embeddings, "embed_one", fake_embed_one)
    monkeypatch.setattr(vector_store, "embed_one", fake_embed_one)

def test_embed_one():
    vec = embeddings.embed_one("synthetic embedding fixture")
    assert isinstance(vec, list)
    assert len(vec) > 0

def test_embed_batch():
    vecs = embeddings.embed(["synthetic alpha", "synthetic beta"])
    assert len(vecs) == 2

def test_upsert_and_search():
    vector_store.upsert_memory(1, "synthetic alpha", agent_id="test")
    vector_store.upsert_memory(2, "synthetic beta", agent_id="test")
    vector_store.upsert_memory(3, "synthetic gamma", agent_id="test")
    results = vector_store.search_similar("synthetic", limit=2, agent_id="test")
    assert len(results) > 0

def test_agent_isolation():
    vector_store.upsert_memory(1, "agent alpha memory", agent_id="alpha")
    vector_store.upsert_memory(1, "agent beta memory", agent_id="beta")
    alpha_results = vector_store.search_similar("memory", limit=5, agent_id="alpha")
    beta_results = vector_store.search_similar("memory", limit=5, agent_id="beta")
    assert alpha_results[0]["content"] == "agent alpha memory"
    assert beta_results[0]["content"] == "agent beta memory"


def test_client_rebuilds_when_chroma_path_changes(tmp_path, monkeypatch):
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"

    monkeypatch.setenv("CHROMA_PATH", str(first_path))
    first_client = vector_store._get_client()
    monkeypatch.setenv("CHROMA_PATH", str(second_path))
    second_client = vector_store._get_client()

    assert second_client is not first_client
    assert vector_store._client_path == str(second_path)


def test_reset_client_uses_public_close(monkeypatch):
    class FakeClient:
        closed = False

        def close(self):
            self.closed = True

    fake_client = FakeClient()
    monkeypatch.setattr(vector_store, "_client", fake_client)
    monkeypatch.setattr(vector_store, "_client_path", "synthetic-path")
    monkeypatch.setattr(vector_store, "_collection", object())

    vector_store.reset_client()

    assert fake_client.closed is True
    assert vector_store._client is None
    assert vector_store._client_path is None
    assert vector_store._collection is None
