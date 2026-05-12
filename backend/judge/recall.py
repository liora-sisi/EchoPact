import math
from datetime import datetime, timezone
from typing import List, Dict
from ..utils.db import get_conn

HALF_LIFE_DAYS = 7.0
HIGH_EMOTION_THRESHOLD = 0.5
HIGH_EMOTION_HALF_LIFE_DAYS = 30.0
UNDONE_BONUS = 0.3

def _time_decay(created_at: str, emotion_weight: float) -> float:
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_elapsed = (now - created).total_seconds() / 86400.0
    except Exception:
        days_elapsed = 0.0
    half_life = HIGH_EMOTION_HALF_LIFE_DAYS if emotion_weight >= HIGH_EMOTION_THRESHOLD else HALF_LIFE_DAYS
    return math.pow(0.5, days_elapsed / half_life)

def _keyword_score(content: str, query: str) -> float:
    if not query.strip():
        return 1.0
    keywords = query.strip().split()
    hits = sum(1 for kw in keywords if kw.lower() in content.lower())
    return hits / len(keywords) if keywords else 1.0

def recall_memories(query: str, limit: int = 5) -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, valence, arousal, importance, "
            "recall_count, calculated_weight, created_at, is_done "
            "FROM memories ORDER BY created_at DESC"
        ).fetchall()

    results = []
    for row in rows:
        valence = row["valence"] or 0.0
        arousal = row["arousal"] or 0.0
        importance = row["importance"] or 0.5
        is_done = bool(row["is_done"])

        emotion_weight = max(0, (valence + 1) / 2) * arousal
        decay = _time_decay(row["created_at"], emotion_weight)
        undone_bonus = UNDONE_BONUS if not is_done else 0.0
        keyword_score = _keyword_score(row["content"], query)

        final_weight = (
            emotion_weight * 0.3 +
            importance * 0.2 +
            decay * 0.2 +
            undone_bonus * 0.1 +
            keyword_score * 0.2
        )

        results.append({
            "id": row["id"],
            "content": row["content"],
            "weight": round(final_weight, 4)
        })

    results.sort(key=lambda x: x["weight"], reverse=True)
    return results[:limit]
