from typing import List

EMOTION_MAP = {
    # 正面
    "开心": (0.8, 0.6, "self"),
    "感动": (0.9, 0.4, "other"),
    "期待": (0.7, 0.7, "event"),
    "满足": (0.8, 0.2, "self"),
    # 负面
    "难过": (-0.7, 0.3, "self"),
    "生气": (-0.8, 0.8, "other"),
    "焦虑": (-0.6, 0.7, "event"),
    "失望": (-0.7, 0.2, "other"),
    # 中性
    "平静": (0.0, 0.1, "self"),
    "好奇": (0.3, 0.5, "event"),
}

def emotion_hint(keywords: List[str]):
    """
    根据关键词列表猜测情感坐标
    返回 (valence, arousal, direction)
    找不到就返回默认值
    """
    for kw in keywords:
        if kw in EMOTION_MAP:
            return EMOTION_MAP[kw]
    return (0.0, 0.0, "self")

def parse_tags(tag_str: str) -> List[str]:
    """逗号分隔的标签字符串 → 列表"""
    if not tag_str:
        return []
    return [t.strip() for t in tag_str.split(",") if t.strip()]

def tags_to_str(tags: List[str]) -> str:
    """列表 → 逗号分隔字符串"""
    return ",".join(tags)
