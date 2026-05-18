from datetime import datetime, timezone
from typing import Optional
from ..utils.db import get_conn
import uuid

def calc_confidence(source_type: str, content: str, 
                    agent_id: str = "default") -> float:
    if source_type == "user":
        return 1.0
    
    base = 0.6
    boost = 0.0
    
    with get_conn() as conn:
        # 被用户重复提及>=2次
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM memories "
            "WHERE content LIKE ? AND source_type='user' AND agent_id=?",
            (f"%{content[:10]}%", agent_id)
        ).fetchone()["cnt"]
        if count >= 2:
            boost += 0.2

    return min(0.95, base + boost)

def check_and_register_conflict(
    new_id: int, content: str, 
    valence: float, agent_id: str = "default"
) -> Optional[str]:
    """检测新记忆是否与已有记忆冲突，返回conflict_group_id"""
    with get_conn() as conn:
        # 找情感倾向相反的记忆
        if valence > 0.3:
            rows = conn.execute(
                "SELECT id FROM memories "
                "WHERE valence < -0.3 AND agent_id=? "
                "AND conflict_group_id IS NULL",
                (agent_id,)
            ).fetchall()
        elif valence < -0.3:
            rows = conn.execute(
                "SELECT id FROM memories "
                "WHERE valence > 0.3 AND agent_id=? "
                "AND conflict_group_id IS NULL",
                (agent_id,)
            ).fetchall()
        else:
            return None

        if not rows:
            return None

        group_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()

        for row in rows[:2]:
            conn.execute(
                "UPDATE memories SET conflict_group_id=? WHERE id=?",
                (group_id, row["id"])
            )
            conn.execute(
                "INSERT INTO memory_conflicts "
                "(conflict_id, group_id, fact1_id, fact2_id, "
                "conflict_type, created_at, resolved) "
                "VALUES (?,?,?,?,'value_diff',?,0)",
                (str(uuid.uuid4())[:8], group_id, 
                 row["id"], new_id, now)
            )
        
        conn.execute(
            "UPDATE memories SET conflict_group_id=? WHERE id=?",
            (group_id, new_id)
        )
        return group_id
