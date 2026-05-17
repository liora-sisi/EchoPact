import os
import requests
from typing import List

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-4b20c9654ad84e68963eefae2463b0d6")

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

def embed(texts: List[str]) -> List[List[float]]:
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    response = requests.post(
        f"{DEEPSEEK_BASE_URL}/embeddings",
        headers=headers,
        json={
            "model": "deepseek-embedding-v2",
            "input": texts
        }
    )
    data = response.json()
    return [item["embedding"] for item in data["data"]]

def embed_one(text: str) -> List[float]:
    return embed([text])[0]
