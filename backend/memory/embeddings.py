import hashlib
import os
import random
from typing import List

import requests
from dotenv import load_dotenv

load_dotenv()

EMBEDDING_DIM = 1536

# —— 防燃烧熔断器（y_patch_2026-05-30 第六节建议的落地实现）——
# 单次进程内真实 API 请求数超过阈值直接中断，防止循环误调烧钱。
_real_call_count = 0


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() == "true"


def _real_embedding_enabled() -> bool:
    # 双开关：两个都显式为 true 才允许真实调用（y_patch 原则2）
    return _env_true("USE_REAL_EMBEDDING") and _env_true("ALLOW_REAL_API_CALLS")


def _real_api_max_calls() -> int:
    try:
        return int(os.getenv("REAL_API_MAX_CALLS", "50"))
    except ValueError:
        return 50


def _mock_vector(text: str) -> List[float]:
    """确定性伪向量：同一文本永远同一向量，不同文本向量不同。

    让 mock 模式下的相似度排序也有真实区分度（旧版所有文本共用
    同一向量，余弦相似度全为 1，排序无意义）。
    """
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIM)]


def embed(texts: List[str]) -> List[List[float]]:
    global _real_call_count

    if not _real_embedding_enabled():
        return [_mock_vector(t) for t in texts]

    print("⚠️ 真实 embedding 已启用，注意 token/API 消耗", flush=True)

    _real_call_count += 1
    max_calls = _real_api_max_calls()
    if _real_call_count > max_calls:
        raise RuntimeError(
            f"熔断：本次进程真实 embedding 请求已达 {max_calls} 次上限。"
            f"如确属正常用量，请调大 REAL_API_MAX_CALLS。"
        )

    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_api_key:
        raise RuntimeError("USE_REAL_EMBEDDING=true 但 OPENAI_API_KEY 为空")

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
