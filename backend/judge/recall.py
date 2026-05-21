import math
from datetime import datetime, timezone
from typing import List, Dict
from ..utils.db import get_conn
from ..memory.vector_store import search_similar

HALF_LIFE_DAYS = 7.0
HIGH_EMOTION_THRESHOLD = 0.5
HIGH_EMOTION_HALF_LIFE_DAYS = 30.0
UNDONE_BONUS = 0.3

def _build_explanation(sim, emotion_fit, decay, saga_boost, undone_bonus) -> str:
    reasons = []
    if sim > 0.6:
        reasons.append("语义高度相关")
    elif sim > 0.3:
        reasons.append("语义有关联")
    if emotion_fit > 0.7:
        reasons.append("情绪契合")
    if decay > 0.8:
        reasons.append("记忆较新")
    elif decay < 0.3:
        reasons.append("记忆较久远")
    if saga_boost > 1.0:
        reasons.append("主线Saga加权")
    if undone_bonus > 0:
        reasons.append("未完成事项")
    return "，".join(reasons) if reasons else "综合权重召回"

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
    from ..memory.embeddings import embed_one
    import os
    
    use_real = os.getenv("USE_REAL_EMBEDDING", "false").lower() == "true"
    
    if use_real:
        query_vec = embed_one(query)
    else:
        query_vec = [0.5] * 1536

    vector_results = search_similar(query, limit=limit * 2, agent_id=agent_id)
    vector_ids = {r["id"]: 1.0 - r["distance"] for r in vector_results}

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, valence, arousal, importance, "
            "recall_count, calculated_weight, created_at, is_done, saga_id "
            "SELECT id, content, valence, arousal, importance, "
            "recall_count, calculated_weight, created_at, is_done, saga_id, recall_reason "
            "FROM memories WHERE agent_id = ? ORDER BY created_at DESC",
            (agent_id,)
        ).fetchall()

    results = []
    for row in rows:
        valence = row["valence"] or 0.0
        arousal = row["arousal"] or 0.0
        importance = row["importance"] or 0.5
        is_done = bool(row["is_done"])

        # 1. 主题关联度（向量相似度）
        sim = vector_ids.get(row["id"], 0.5)

        # 2. 情绪契合度
        emotion_diff = abs(valence - 0.0)
        emotion_fit = max(0.0, 1.0 - emotion_diff)

        # 3. 时间衰减
        emotion_weight = max(0, (valence + 1) / 2) * arousal
        decay = _time_decay(row["created_at"], emotion_weight)

        # 4. Saga权重
        saga_boost = 1.5 if row["saga_id"] else 1.0

        # 未完成加分
        undone_bonus = 0.3 if not is_done else 0.0

        final_weight = (
            0.35 * sim +
            0.25 * emotion_fit +
            0.20 * decay +
            0.20 * saga_boost
        ) + undone_bonus

        results.append({
            "id": row["id"],
            "content": row["content"],
            "weight": round(final_weight, 4),
            "reason": {
                "sim": round(sim, 4),
                "emotion_fit": round(emotion_fit, 4),
                "decay": round(decay, 4),
                "saga_boost": round(saga_boost, 4),
                "undone_bonus": round(undone_bonus, 4),
                "recall_reason": row["recall_reason"] if row["recall_reason"] else "此条暂无温度",
                "explanation": _build_explanation(sim, emotion_fit, decay, saga_boost, undone_bonus)
            },
        })

    results.sort(key=lambda x: x["weight"], reverse=True)
    return results[:limit]
