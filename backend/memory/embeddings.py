import hashlib
import os
import random
from typing import List

import requests
from dotenv import load_dotenv

load_dotenv()

EMBEDDING_DIM = 1536

_real_call_count = 0


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() == "true"


def _real_embedding_enabled() -> bool:
    return _env_true("USE_REAL_EMBEDDING") and _env_true("ALLOW_REAL_API_CALLS")


def _real_api_max_calls() -> int:
    try:
        return int(os.getenv("REAL_API_MAX_CALLS", "50"))
    except ValueError:
        return 50


def _mock_vector(text: str) -> List[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIM)]


def embed(texts: List[str]) -> List[List[float]]:
    global _real_call_count

    if not _real_embedding_enabled():
        return [_mock_vector(t) for t in texts]

    print("WARNING: real embedding is enabled; API usage may incur cost", flush=True)

    _real_call_count += 1
    max_calls = _real_api_max_calls()
    if _real_call_count > max_calls:
        raise RuntimeError(
            f"Real embedding call limit reached: {max_calls}. "
            "Increase REAL_API_MAX_CALLS only after confirming this is expected."
        )

    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_api_key:
        raise RuntimeError("USE_REAL_EMBEDDING=true but OPENAI_API_KEY is empty")

    response = requests.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {openai_api_key}",
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
