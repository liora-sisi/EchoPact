from sentence_transformers import SentenceTransformer
from typing import List
import os

MODEL_NAME = os.getenv("EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
_model = None

def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model

def embed(texts: List[str]) -> List[List[float]]:
    model = get_model()
    return model.encode(texts, normalize_embeddings=True).tolist()

def embed_one(text: str) -> List[float]:
    return embed([text])[0]
