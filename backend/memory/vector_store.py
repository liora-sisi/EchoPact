import chromadb
import os
from typing import List, Dict
from .embeddings import embed_one, embed

CHROMA_PATH = os.getenv("CHROMA_PATH", "/opt/echo-pact/chroma_db")

_client = None
_collection = None

def get_collection(agent_id: str = "default"):
    global _client, _collection
    if _client is None:
        _client = chromadb.PersistentClient(CHROMA_PATH = os.getenv("CHROMA_PATH", "/opt/echo-pact/chroma_db"))
    collection_name = f"memories_{agent_id}"
    return _client.get_or_create_collection(
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
