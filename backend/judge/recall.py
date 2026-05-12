import sqlite3
import math
from datetime import datetime, timedelta
from typing import List, Dict, Any

DB_PATH = "/opt/echo-pact/memories.db"

def get_conn():
    return sqlite3.connect(DB_PATH)

def recall_memories(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    召回记忆：结合时间衰减、情绪权重、未完成优先级
    """
    conn = get_conn()
    c = conn.cursor()
    # 获取所有未删除的记忆（假设 done=0 为未完成）
    c.execute("""
        SELECT id, content, valence, arousal, importance, recall_count, created_at, done
        FROM memories
        WHERE done = 0 OR done IS NULL
    """)
    rows = c.fetchall()
    conn.close()

    now = datetime.now()
    scored = []
    for row in rows:
        mem_id, content, valence, arousal, imp, recall_cnt, created_at_str, done = row
        created_at = datetime.fromisoformat(created_at_str)
        days = (now - created_at).days
        # 时间衰减：半衰期7天
        time_weight = math.exp(-days / 7.0)
        # 情绪权重：valence * arousal，归一化到0-1
        emotion_weight = max(0, (valence + 1) / 2) * arousal
        # 未完成事项加成
        undone_boost = 0.5 if done == 0 else 0.0
        # 原始权重（importance * 时间衰减 * 情绪权重）
        raw_weight = imp * time_weight * emotion_weight
        # 最终权重
        final_weight = raw_weight + undone_boost
        scored.append((final_weight, content, mem_id))

    # 按权重降序排序
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"id": mem_id, "content": content, "weight": round(w, 4)} for w, content, mem_id in scored[:limit]]
