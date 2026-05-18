from datetime import datetime
from typing import Optional, List
from .models import Memory
from ..utils.db import get_conn

IMPORTANCE_KEYWORDS = {
    "保长": 0.2, "master": 0.2, "催": 0.2,
    "哭": 0.3, "难过": 0.3,
}

def estimate_importance(content: str) -> float:
    score = 0.5
    for kw, boost in IMPORTANCE_KEYWORDS.items():
        if kw in content:
            score += boost
    return min(score, 1.0)

def create_memory(mem) -> int:
    if isinstance(mem, dict):
        mem = Memory(**mem)
    mem.importance = estimate_importance(mem.content)
    d = mem.to_dict()
    with get_conn() as conn:
        cur = conn.execute(
            '''INSERT INTO memories
            (content, summary, valence, arousal, direction, tags, is_done,
             decay_category, importance, recall_count, calculated_weight,
             agent_id, source_type, confidence, conflict_group_id, last_verified_at,
             created_at)
            VALUES (:content, :summary, :valence, :arousal, :direction, :tags, :is_done,
             :decay_category, :importance, :recall_count, :calculated_weight,
             :agent_id, :source_type, :confidence, :conflict_group_id, :last_verified_at,
             :created_at)''',
           d
        )
        return cur.lastrowid

def get_memory(memory_id: int) -> Optional[Memory]:
    with get_conn() as conn:
        conn.execute(
            "UPDATE memories SET recall_count = recall_count + 1, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), memory_id)
        )
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return _row_to_memory(row) if row else None

def list_memories(limit: int = 20, direction: Optional[str] = None, agent_id: str = "default") -> List[Memory]:
    with get_conn() as conn:
        if direction:
            rows = conn.execute(
                "SELECT * FROM memories WHERE direction = ? AND agent_id = ? ORDER BY created_at DESC LIMIT ?",
                (direction, agent_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
                (agent_id, limit)
            ).fetchall()
        return [_row_to_memory(r) for r in rows]
    with get_conn() as conn:
        if direction:
            rows = conn.execute(
                "SELECT * FROM memories WHERE direction = ? ORDER BY created_at DESC LIMIT ?",
                (direction, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [_row_to_memory(r) for r in rows]

def list_undone(limit: int = 10) -> List[Memory]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM memories WHERE is_done = 0 ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [_row_to_memory(r) for r in rows]

def mark_done(memory_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE memories SET is_done = 1, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), memory_id)
        )

def delete_memory(memory_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

def _row_to_memory(row) -> Memory:
    return Memory(
        id=row["id"],
        content=row["content"],
        summary=row["summary"],
        valence=row["valence"],
        arousal=row["arousal"],
        direction=row["direction"],
        tags=row["tags"].split(",") if row["tags"] else [],
        is_done=bool(row["is_done"]),
        decay_category=row["decay_category"],
        importance=row["importance"],
        recall_count=row["recall_count"],
        calculated_weight=row["calculated_weight"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
