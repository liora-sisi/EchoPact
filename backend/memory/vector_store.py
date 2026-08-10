import chromadb
import os
from typing import List, Dict
from .embeddings import embed_one, embed

CHROMA_PATH = os.getenv("CHROMA_PATH", "/opt/echo-pact/chroma_db")

_client = None
_client_path = None
_collection = None


def _current_path() -> str:
    return os.getenv("CHROMA_PATH", "/opt/echo-pact/chroma_db")


def _get_client():
    """Return a persistent client bound to the current CHROMA_PATH.

    A path change creates a fresh client instead of reusing storage from an
    earlier test or process configuration. Path errors remain visible to the
    caller; there is no in-memory fallback.
    """
    global _client, _client_path
    path = _current_path()
    if _client is None or _client_path != path:
        reset_client()
        _client = chromadb.PersistentClient(path=path)
        _client_path = path
    return _client


def reset_client():
    """Close and forget the cached client and collection.

    Chroma 1.5 exposes ``close()`` for releasing persistent resources. Older
    supported clients may not provide it; in that case dropping the cached
    reference preserves their historical behavior. A real close failure is
    deliberately allowed to surface.
    """
    global _client, _client_path, _collection
    if _client is not None:
        close = getattr(_client, "close", None)
        if callable(close):
            close()
    _client = None
    _client_path = None
    _collection = None


def get_collection(agent_id: str = "default"):
    client = _get_client()
    collection_name = f"memories_{agent_id}"
    return client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"}
    )

def upsert_memory(memory_id: int, content: str, agent_id: str = "default"):
    collection = get_collection(agent_id)
    vector = embed_one(content)
    collection.upsert(
        ids=[str(memory_id)],
        embeddings=[vector],
        documents=[content]
    )

def search_similar(query: str, limit: int = 5, agent_id: str = "default") -> List[Dict]:
    collection = get_collection(agent_id)
    if collection.count() == 0:
        return []
    query_vector = embed_one(query)
    results = collection.query(
        query_embeddings=[query_vector],
        n_results=min(limit, collection.count())
    )
    output = []
    for i, doc_id in enumerate(results["ids"][0]):
        output.append({
            "id": int(doc_id),
            "content": results["documents"][0][i],
            "distance": results["distances"][0][i]
        })
    return output
