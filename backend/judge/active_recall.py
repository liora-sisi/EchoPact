import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict
from ..utils.db import get_conn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COOLDOWN_MINUTES = 120
SILENCE_THRESHOLD_MINUTES = 45
CHECK_INTERVAL_MINUTES = 30
def get_dynamic_interval(agent_id: str = "default") -> int:
    try:
        from ..memory.profile import get_push_interval
        return get_push_interval(agent_id)
    except Exception:
        return COOLDOWN_MINUTES

def _active_weight(valence: float, arousal: float, 
                   is_done: int, recall_count: int) -> float:
    undone = 1.0 if is_done == 0 else 0.0
    emotion = (valence ** 2) * arousal
    freshness = 1.0 - min(recall_count / 10.0, 1.0)
    return undone * 0.6 + emotion * 0.3 + freshness * 0.1

def _get_last_message_time() -> Optional[datetime]:
    """从interaction_log获取最后消息时间，暂时返回None用当前时间模拟"""
    # TODO: 接入interaction_log表后实现
    return None

def _get_last_push_time() -> Optional[datetime]:
    """获取上次主动推送时间"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM system_meta WHERE key = 'last_active_push'"
        ).fetchone()
        if row:
            return datetime.fromisoformat(row["value"])
    return None

def _update_last_push_time():
    with get_conn() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO system_meta (key, value) VALUES ('last_active_push', ?)",
            (now,)
        )

def _ensure_meta_table():
    with get_conn() as conn:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS system_meta "
            "(key TEXT PRIMARY KEY, value TEXT);"
        )

def pick_memory_to_push() -> Optional[Dict]:
    """选出最该主动推送的一条记忆"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, valence, arousal, is_done, recall_count "
            "FROM memories ORDER BY created_at DESC"
        ).fetchall()

    if not rows:
        return None

    best = None
    best_score = -1.0

    for row in rows:
        score = _active_weight(
            row["valence"] or 0.0,
            row["arousal"] or 0.0,
            row["is_done"] or 0,
            row["recall_count"] or 0
        )
        if score > best_score:
            best_score = score
            best = {"id": row["id"], "content": row["content"], "weight": round(score, 4)}

    return best

def should_push(last_message_time: Optional[datetime] = None) -> bool:
    """判断是否需要主动推送"""
    now = datetime.now(timezone.utc)

    if last_message_time is None:
        last_message_time = now - timedelta(minutes=SILENCE_THRESHOLD_MINUTES + 1)

    silence_minutes = (now - last_message_time).total_seconds() / 60
    if silence_minutes < SILENCE_THRESHOLD_MINUTES:
        logger.info(f"沉默时间{silence_minutes:.1f}分钟，未到阈值，不推送")
        return False

    last_push = _get_last_push_time()
    if last_push:
        cooldown_minutes = (now - last_push).total_seconds() / 60
        dynamic_interval = get_dynamic_interval()
        if cooldown_minutes < dynamic_interval:
            logger.info(f"冷却中，距上次推送{cooldown_minutes:.1f}分钟，不推送")
            return False
    # 推断用户状态
    from ..memory.context import infer_user_state
    user_state = infer_user_state()
    if user_state:
        logger.info(f"用户状态推断：{user_state}")
    
    return True
    return True

def run_active_recall(last_message_time: Optional[datetime] = None):
    """主动召回主函数，由cron每30分钟调用"""
    _ensure_meta_table()

    if not should_push(last_message_time):
        return

    memory = pick_memory_to_push()
    if not memory:
        logger.info("记忆库为空，无可推送内容")
        return

    _update_last_push_time()
    logger.info(f"[主动浮现] 推送记忆ID={memory['id']} | 权重={memory['weight']}")
    logger.info(f"[主动浮现] 内容：{memory['content']}")
    # TODO: 接入LobeChat API后在这里发送推送

if __name__ == "__main__":
    run_active_recall()
