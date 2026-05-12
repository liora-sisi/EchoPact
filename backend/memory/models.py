from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List

@dataclass
class Memory:
    content: str
    valence: float = 0.0      # 效价 -1(负面) ～ +1(正面)
    arousal: float = 0.0      # 唤醒度 0(平静) ～ 1(激烈)
    direction: str = "self"   # 指向性: "self" / "other" / "event"
    summary: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    is_done: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: Optional[str] = None
    id: Optional[int] = None

    def emotion_weight(self) -> float:
        """情绪强度 = |效价| × 唤醒度，越极端越容易浮现"""
        return abs(self.valence) * self.arousal

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "summary": self.summary,
            "valence": self.valence,
            "arousal": self.arousal,
            "direction": self.direction,
            "tags": ",".join(self.tags),
            "is_done": int(self.is_done),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
