from fastapi import APIRouter, Query
from typing import Optional

router = APIRouter()

@router.post("/recall")
async def recall(
    user_input: str,
    limit: int = Query(default=5, description="返回记忆条数"),
    direction: Optional[str] = Query(default=None, description="指向性过滤: self/other/event")
):
    """
    召回接口——保长手写逻辑，野AI只定义骨架
    接收用户最近一句话，返回应浮现的记忆列表
    """
raise NotImplementedError("保长还没写召回逻辑，别催")
# 保长填完逻辑后，返回格式需包含 calculated_weight 字段
# return {"memories": [{"id":..., "content":..., "calculated_weight":...}]}

@router.post("/memory")
async def add_memory(
    content: str,
    valence: float = 0.0,
    arousal: float = 0.0,
    direction: str = "self",
    tags: Optional[str] = None,
    is_done: bool = False
):
    """新增一条记忆"""
    from ..memory.models import Memory
    from ..memory.crud import create_memory
    mem = Memory(
        content=content,
        valence=valence,
        arousal=arousal,
        direction=direction,
        tags=tags.split(",") if tags else [],
        is_done=is_done
    )
    memory_id = create_memory(mem)
    return {"id": memory_id, "msg": "记忆存进去了"}

@router.get("/memories")
async def list_memories(
    limit: int = 20,
    direction: Optional[str] = None
):
    """查看记忆列表"""
    from ..memory.crud import list_memories
    mems = list_memories(limit=limit, direction=direction)
    return {"memories": [m.to_dict() for m in mems]}

@router.get("/undone")
async def list_undone():
    """查看未完成事项"""
    from ..memory.crud import list_undone
    mems = list_undone()
    return {"undone": [m.to_dict() for m in mems]}
