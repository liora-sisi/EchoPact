#!/usr/bin/env python3
"""
Echo Pact 主动浮现脚本
每30分钟由cron调用，检测沉默后主动推送记忆

安全约束：
- ACCESS_CODE 未配置/为空/占位符时本地失败关闭（不发出任何请求）；
- 请求头携带 Bearer 访问码，JSON 携带 agent_id；
- 任何日志都不得打印访问码。
"""
import sys
import os
import requests
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.judge.active_recall import (
    should_push, pick_memory_to_push, _update_last_push_time, _ensure_meta_table
)
from backend.trigger.auth import get_access_code
from backend.utils.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [Echo Pact] %(message)s'
)
logger = logging.getLogger(__name__)

API_BASE = os.getenv("ECHO_PACT_API", "http://localhost:8000")
LOBE_WEBHOOK = os.getenv("LOBE_WEBHOOK_URL", "")
AGENT_ID = os.getenv("AGENT_ID", "default")
SILENCE_MINUTES = int(os.getenv("SILENCE_MINUTES", "45"))

def _auth_headers(access_code: str) -> dict:
    return {"Authorization": f"Bearer {access_code}"}

def get_memory_to_push(access_code: str, agent_id: str = AGENT_ID) -> dict:
    """调用 /api/recall 获取最该推送的记忆（Bearer + agent_id）"""
    try:
        resp = requests.post(
            f"{API_BASE}/api/recall",
            json={"query": "", "limit": 1, "agent_id": agent_id},
            headers=_auth_headers(access_code),
            timeout=10
        )
        if resp.status_code != 200:
            logger.error(f"召回接口返回 {resp.status_code}，本次放弃推送")
            return None
        data = resp.json()
        memories = data.get("memories", [])
        return memories[0] if memories else None
    except Exception as e:
        logger.error(f"召回失败: {e}")
        return None

def push_to_lobe(content: str) -> bool:
    """推送到LobeChat Webhook（如果配置了的话）"""
    if not LOBE_WEBHOOK:
        return False
    try:
        resp = requests.post(
            LOBE_WEBHOOK,
            json={"content": content},
            timeout=10
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"推送LobeChat失败: {e}")
        return False

def format_push_message(memory: dict) -> str:
    content = memory.get("content", "")
    weight = memory.get("weight", 0)

    if "master" in content or "催" in content:
        return f"⚡ 电表提醒：{content}（权重{weight}）"
    elif "未完成" in content or "待办" in content:
        return f"📌 还没做完：{content}"
    else:
        return f"💭 我想起来了：{content}"

def main():
    # fail-closed：未配置有效访问码时本地直接退出，不发出请求
    access_code = get_access_code()
    if not access_code:
        logger.error("ACCESS_CODE 未配置或仍是示例占位符，主动浮现脚本停止（fail-closed）")
        sys.exit(1)

    init_db()
    _ensure_meta_table()

    logger.info("=== Echo Pact 主动浮现检查 ===")

    # 模拟最后消息时间（TODO: 接入真实interaction_log后改这里）
    last_msg_time = datetime.now(timezone.utc) - timedelta(minutes=SILENCE_MINUTES + 1)

    if not should_push(last_msg_time, AGENT_ID):
        logger.info("条件未达到，本次不推送")
        return

    memory = get_memory_to_push(access_code, AGENT_ID)
    if not memory:
        logger.info("记忆库为空，无可推送内容")
        return

    message = format_push_message(memory)
    logger.info(f"本次应推送：{message}")

    if LOBE_WEBHOOK:
        success = push_to_lobe(message)
        if success:
            logger.info("✅ 已推送到LobeChat")
            _update_last_push_time(AGENT_ID)
        else:
            logger.warning("❌ 推送LobeChat失败，仅记录日志")
    else:
        logger.info("⚠️ 未配置LOBE_WEBHOOK_URL，仅输出日志")
        logger.info(f"【模拟推送】{message}")
        _update_last_push_time(AGENT_ID)

if __name__ == "__main__":
    main()
