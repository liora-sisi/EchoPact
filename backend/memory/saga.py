from datetime import datetime, timezone
from typing import Optional, List, Dict
from ..utils.db import get_conn

def create_saga(title: str, agent_id: str = "default") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sagas (title, status, agent_id, created_at) "
            "VALUES (?, 'active', ?, ?)",
            (title, agent_id, datetime.now(timezone.utc).isoformat())
        )
        return cur.lastrowid

def complete_saga(saga_id: int, agent_id: str = "default"):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sagas SET status = 'completed', updated_at = ? WHERE id = ? AND agent_id = ?",
            (datetime.now(timezone.utc).isoformat(), saga_id, agent_id)
        )
        if cur.rowcount == 0:
            # 不存在与跨 agent 共用同一措辞，不泄露记录归属
            raise ValueError("Saga 不存在或不属于当前 agent")

def list_sagas(agent_id: str = "default", status: str = "active") -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sagas WHERE agent_id = ? AND status = ? ORDER BY created_at DESC",
            (agent_id, status)
        ).fetchall()
    return [dict(r) for r in rows]

def get_saga(saga_id: int, agent_id: str = "default") -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sagas WHERE id = ? AND agent_id = ?", (saga_id, agent_id)
        ).fetchone()
    return dict(row) if row else None
