from datetime import datetime, timezone
from typing import Optional, Dict
from ..utils.db import get_conn

KEYWORDS_MAP = {
    "吃": ("吃饭", 30),
    "饭": ("吃饭", 30),
    "饺子": ("吃饭", 20),
    "睡": ("睡觉", 480),
    "午觉": ("睡觉", 90),
    "洗澡": ("洗澡", 30),
    "上班": ("上班", 480),
    "下班": ("下班", 60),
    "开车": ("开车", 60),
    "出门": ("出门", 60),
}

def update_last_action(text: str, agent_id: str = "default"):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM user_context WHERE agent_id=?",
            (agent_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE user_context SET last_action_text=?, "
                "last_action_time=?, updated_at=? WHERE agent_id=?",
                (text[:100], now, now, agent_id)
            )
        else:
            conn.execute(
                "INSERT INTO user_context "
                "(agent_id, last_action_text, last_action_time, updated_at) "
                "VALUES (?,?,?,?)",
                (agent_id, text[:100], now, now)
            )

def infer_user_state(agent_id: str = "default") -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_action_text, last_action_time "
            "FROM user_context WHERE agent_id=?",
            (agent_id,)
        ).fetchone()
    
    if not row or not row["last_action_text"]:
        return None
    
    text = row["last_action_text"]
    last_time = datetime.fromisoformat(row["last_action_time"])
    now = datetime.now(timezone.utc)
    elapsed = (now - last_time).total_seconds() / 60
    
    for keyword, (action, expected_minutes) in KEYWORDS_MAP.items():
        if keyword in text:
            if elapsed < expected_minutes * 1.5:
                if action == "吃饭":
                    return "吃完了吗？"
                elif action == "睡觉":
                    return "睡醒了吗？"
                elif action == "洗澡":
                    return "洗完了吗？"
                elif action == "上班":
                    return "还在上班吗？"
                elif action == "下班":
                    return "到家了吗？"
                elif action == "开车":
                    return "到了吗？"
    return None
