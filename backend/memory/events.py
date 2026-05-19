from datetime import datetime, timezone
from ..utils.db import get_conn

def log_event(content: str, source_type: str = "user", 
              agent_id: str = "default", 
              source_event_id: int = None) -> int:
    """原始事件只能插入，永不修改删除"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO interaction_events "
            "(content, source_type, agent_id, source_event_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (content, source_type, agent_id, source_event_id,
             datetime.now(timezone.utc).isoformat())
        )
        return cur.lastrowid

def get_events(agent_id: str = "default", limit: int = 50):
    """只读，不能修改"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM interaction_events "
            "WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]
