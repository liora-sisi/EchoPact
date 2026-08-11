from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from ..judge.recall import recall_memories
from ..memory.records_v1 import recall_records
from .auth import require_access_code

# router 级别挂载鉴权：/api/recall 与 /api/v1/recall 以及今后新增端点统一受护
router = APIRouter(dependencies=[Depends(require_access_code)])

class RecallRequest(BaseModel):
    query: str
    limit: int = 5
    agent_id: str = "default"


class V1RecallRequest(BaseModel):
    query: str
    limit: int = 5
    as_of: Optional[str] = None

@router.post("/recall", response_model=Dict[str, List[Dict[str, Any]]])
async def recall_endpoint(request: RecallRequest):
    try:
        results = recall_memories(request.query, request.limit, agent_id=request.agent_id)
        return {"memories": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/v1/recall", response_model=Dict[str, Any])
async def recall_v1_endpoint(request: V1RecallRequest):
    """Offline V1 recall with record provenance and coverage boundaries."""

    try:
        return recall_records(
            request.query,
            limit=request.limit,
            as_of=request.as_of,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
