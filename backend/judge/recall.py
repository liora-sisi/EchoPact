import math
from datetime import datetime, timezone
from typing import List, Dict
from ..utils.db import get_conn
from ..memory.vector_store import search_similar

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

def recall_memories(query: str, limit: int = 5, agent_id: str = "default") -> List[Dict]:
    # 向量语义检索
    vector_results = search_similar(query, limit=limit * 2, agent_id=agent_id)
    vector_ids = {r["id"]: 1.0 - r["distance"] for r in vector_results}

    # 从数据库读取候选记忆
    with get_conn() as conn:
        rows = conn.execute(
        "SELECT id, content, valence, arousal, importance, "
        "recall_count, calculated_weight, created_at, is_done, saga_id "
        "FROM memories WHERE agent_id = ? ORDER BY created_at DESC",
            (agent_id,)
        ).fetchall()

    results = []
    for row in rows:
        valence = row["valence"] or 0.0
        arousal = row["arousal"] or 0.0
        importance = row["importance"] or 0.5
        is_done = bool(row["is_done"])

        emotion_weight = max(0, (valence + 1) / 2) * arousal
        saga_boost = 1.5 if row["saga_id"] else 1.0
        decay = _time_decay(row["created_at"], emotion_weight)
        undone_bonus = UNDONE_BONUS if not is_done else 0.0
        semantic_score = vector_ids.get(row["id"], 0.0)

        final_weight = (
            emotion_weight * 0.25 +
            importance * 0.15 +
            decay * 0.15 +
            undone_bonus * 0.1 +
            semantic_score * 0.35
        )* saga_boost

        results.append({
            "id": row["id"],
            "content": row["content"],
            "weight": round(final_weight, 4),
            "reason": {
                "semantic_score": round(semantic_score, 4),
                "emotion_weight": round(emotion_weight, 4),
                "importance": round(importance, 4),
                "decay": round(decay, 4),
                "undone_bonus": round(undone_bonus, 4),
                "saga_boost": round(saga_boost, 4),
            }
      })
    results.sort(key=lambda x: x["weight"], reverse=True)
    return results[:limit]
