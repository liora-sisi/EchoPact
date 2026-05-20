from enum import Enum
from dataclasses import dataclass
from typing import Optional

class TriggerLevel(Enum):
    L1 = "light"      # 轻提醒
    L2 = "goal"       # 目标推进
    L3 = "urgent"     # 高优先告警

@dataclass
class TriggerConfig:
    level: TriggerLevel
    min_interval_minutes: int
    daily_limit: int
    suppression_decay: float  # 负反馈抑制系数

L1_CONFIG = TriggerConfig(
    level=TriggerLevel.L1,
    min_interval_minutes=60,
    daily_limit=5,
    suppression_decay=0.5
)

L2_CONFIG = TriggerConfig(
    level=TriggerLevel.L2,
    min_interval_minutes=120,
    daily_limit=3,
    suppression_decay=0.7
)

L3_CONFIG = TriggerConfig(
    level=TriggerLevel.L3,
    min_interval_minutes=30,
    daily_limit=2,
    suppression_decay=0.9
)

def get_trigger_level(
    has_saga: bool = False,
    is_urgent: bool = False,
    user_state: Optional[str] = None
) -> TriggerConfig:
    if is_urgent:
        return L3_CONFIG
    if has_saga:
        return L2_CONFIG
    return L1_CONFIG
