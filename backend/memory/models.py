from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List

@dataclass
class Memory:
    content: str
    valence: float = 0.0
    arousal: float = 0.0
    direction: str = "self"
    summary: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    is_done: bool = False
    source_type: str = "user"        # user/model/tool/system
    confidence: float = 1.0          # 0~1，置信度
    conflict_group_id: Optional[str] = None  # 冲突组ID
    last_verified_at: Optional[str] = None   # 最后验证时间
    agent_id: str = "default"
    decay_category: str = "fact"
    importance: float = 0.5
    recall_count: int = 0
    calculated_weight: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: Optional[str] = None
    id: Optional[int] = None

    def emotion_weight(self) -> float:
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
            "agent_id": self.agent_id,
            "source_type": self.source_type,
            "confidence": self.confidence,
            "conflict_group_id": self.conflict_group_id,
            "last_verified_at": self.last_verified_at,
            "decay_category": self.decay_category,
            "importance": self.importance,
            "recall_count": self.recall_count,
            "calculated_weight": self.calculated_weight,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
