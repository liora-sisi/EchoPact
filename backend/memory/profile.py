from datetime import datetime, timezone
from typing import Optional, Dict
from ..utils.db import get_conn

DEFAULT_CONSCIENTIOUSNESS = 0.5

def get_profile(agent_id: str = "default") -> Dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_profile WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
    if row:
        return dict(row)
    return {
        "agent_id": agent_id,
        "conscientiousness": DEFAULT_CONSCIENTIOUSNESS,
        "last_updated": None
    }

def update_conscientiousness(delta: float, agent_id: str = "default"):
    profile = get_profile(agent_id)
    new_score = max(0.0, min(1.0, profile["conscientiousness"] + delta))
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_profile "
            "(agent_id, conscientiousness, last_updated) VALUES (?, ?, ?)",
            (agent_id, new_score, datetime.now(timezone.utc).isoformat())
        )
    return new_score

def master_written(agent_id: str = "default") -> float:
    """牛牛写完master，尽责性+0.02"""
    return update_conscientiousness(0.02, agent_id)

def master_delayed(agent_id: str = "default") -> float:
    """牛牛拖更，尽责性-0.01"""
    return update_conscientiousness(-0.01, agent_id)

def get_push_interval(agent_id: str = "default") -> int:
    """根据尽责性返回推送间隔（分钟）"""
    profile = get_profile(agent_id)
    c = profile["conscientiousness"]
    if c < 0.3:
        return 15
    elif c > 0.7:
        return 120
    else:
        return 60
