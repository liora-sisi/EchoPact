import os
import requests
from typing import List
from dotenv import load_dotenv
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

def embed(texts: List[str]) -> List[List[float]]:
    response = requests.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "text-embedding-3-small",
            "input": texts
        }
    )
    data = response.json()
    return [item["embedding"] for item in data["data"]]

def embed_one(text: str) -> List[float]:
    return embed([text])[0]
