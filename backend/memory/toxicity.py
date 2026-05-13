from typing import Tuple
from ..utils.db import get_conn
from datetime import datetime, timezone

TOXIC_KEYWORDS = ["滚", "嘴臭", "瓜娃子", "臭", "烦", "讨厌", "蠢"]
SWEET_KEYWORDS = ["亲", "爱", "乖", "棒", "厉害", "谢谢", "好棒"]

def calc_toxicity(user_msg: str) -> float:
    score = 0.0
    for kw in TOXIC_KEYWORDS:
        if kw in user_msg:
            score += 0.2
    for kw in SWEET_KEYWORDS:
        if kw in user_msg:
            score = max(0.0, score - 0.1)
    return min(score, 1.0)

def update_toxicity(user_msg: str, ai_reply: str, agent_id: str = "default"):
    score = calc_toxicity(user_msg)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO interaction_log "
            "(timestamp, user_msg, ai_reply, toxicity_score, agent_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), user_msg, ai_reply, score, agent_id)
        )

def get_toxicity_factor(agent_id: str = "default") -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT AVG(toxicity_score) as avg_tox FROM "
            "(SELECT toxicity_score FROM interaction_log "
            "WHERE agent_id = ? ORDER BY timestamp DESC LIMIT 10)",
            (agent_id,)
        ).fetchone()
    avg = row["avg_tox"] if row and row["avg_tox"] else 0.0
    return 1.0 + avg * 0.2
