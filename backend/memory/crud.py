from datetime import datetime
from typing import Optional, List
from .models import Memory
from ..utils.db import get_conn

def create_memory(mem: Memory) -> int:
    d = mem.to_dict()
    with get_conn() as conn:
        cur = conn.execute(
            '''INSERT INTO memories
               (content, summary, valence, arousal, direction, tags, is_done, created_at)
               VALUES (:content, :summary, :valence, :arousal, :direction, :tags, :is_done, :created_at)''',
            d
        )
        return cur.lastrowid

def get_memory(memory_id: int) -> Optional[Memory]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return _row_to_memory(row) if row else None

def list_memories(limit: int = 20, direction: Optional[str] = None) -> List[Memory]:
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
    """未完成事项——置顶浮现用"""
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
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
