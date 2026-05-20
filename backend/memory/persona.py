import os
from datetime import datetime, timezone
from typing import Dict
from ..utils.db import get_conn
from dotenv import load_dotenv
load_dotenv()

DEFAULT_PERSONA = {
    "warmth": 0.7,
    "directness": 0.6,
    "initiative": 0.5
}

def get_persona(agent_id: str = "default") -> Dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT warmth, directness, initiative FROM user_profile "
            "WHERE agent_id=?", (agent_id,)
        ).fetchone()
    if row:
        return {
            "warmth": row["warmth"] or DEFAULT_PERSONA["warmth"],
            "directness": row["directness"] or DEFAULT_PERSONA["directness"],
            "initiative": row["initiative"] or DEFAULT_PERSONA["initiative"]
        }
    return DEFAULT_PERSONA.copy()

def update_persona(agent_id: str = "default",
                   warmth_delta: float = 0,
                   directness_delta: float = 0,
                   initiative_delta: float = 0):
    current = get_persona(agent_id)
    new_warmth = max(0.0, min(1.0, current["warmth"] + warmth_delta))
    new_directness = max(0.0, min(1.0, current["directness"] + directness_delta))
    new_initiative = max(0.0, min(1.0, current["initiative"] + initiative_delta))
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_profile "
            "(agent_id, warmth, directness, initiative, last_updated) "
            "VALUES (?,?,?,?,?)",
            (agent_id, new_warmth, new_directness, new_initiative, now)
        )
    return {"warmth": new_warmth, "directness": new_directness, 
            "initiative": new_initiative}

def adapt_persona_to_context(
    stress_level: float = 0.0,
    is_sprint: bool = False,
    agent_id: str = "default"
) -> Dict:
    """根据情境自动调整人格状态"""
    if stress_level > 0.7:
        # 高压时：升warmth，降directness
        return update_persona(agent_id, 
                            warmth_delta=0.1, 
                            directness_delta=-0.1)
    if is_sprint:
        # 冲刺期：升directness，升initiative
        return update_persona(agent_id,
                            directness_delta=0.1,
                            initiative_delta=0.1)
    return get_persona(agent_id)
