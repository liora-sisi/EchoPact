import os
from typing import List

import requests
from dotenv import load_dotenv

load_dotenv()

USE_REAL_EMBEDDING = os.getenv("USE_REAL_EMBEDDING", "false").lower() == "true"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


def embed(texts: List[str]) -> List[List[float]]:
    if not USE_REAL_EMBEDDING:
        return [[0.1] * 1536 for _ in texts]

    response = requests.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "text-embedding-3-small",
            "input": texts,
        },
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    if "data" not in payload:
        raise ValueError(f"Embedding response missing data field: {payload}")

    return [item["embedding"] for item in payload["data"]]


def embed_one(text: str) -> List[float]:
    return embed([text])[0]
