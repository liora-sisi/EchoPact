from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from ..judge.recall import recall_memories
from ..memory.records_v1 import recall_records
from ..memory.recall_projection import recall_with_projection
from .auth import (
    assert_agent_id_compat,
    require_access_code,
    resolve_principal,
)

# M5-04：不再 router 级统一鉴权。
# - /api/recall          → legacy 冻结门禁 require_access_code（行为不变）；
# - /api/v1/recall 系列  → resolve_principal 派生身份上下文，body 里的
#                          agent_id 只做兼容断言，不产生身份。
router = APIRouter()

class RecallRequest(BaseModel):
    query: str
    limit: int = 5
    agent_id: Optional[str] = None


class V1RecallRequest(BaseModel):
    query: str
    limit: int = 5
    as_of: Optional[str] = None
    # M5-04 兼容断言字段：省略/"default" → agt-legacy；其余必须与认证身份一致
    agent_id: Optional[str] = None

@router.post("/recall", response_model=Dict[str, List[Dict[str, Any]]])
async def recall_endpoint(
    request: RecallRequest,
    principal: str = Depends(resolve_principal),
):
    """Legacy recall with identity derived only from the Bearer token."""
    agent_id = assert_agent_id_compat(request.agent_id, principal)
    # Pre-v5 callers stored the legacy principal under the historical
    # ``default`` namespace. Keep that storage alias without allowing the
    # request body to select an arbitrary identity.
    storage_agent_id = "default" if agent_id == "agt-legacy" else agent_id
    try:
        results = recall_memories(
            request.query, request.limit, agent_id=storage_agent_id
        )
        return {"memories": results}
    except Exception:
        raise HTTPException(status_code=500, detail="Recall failed")


@router.post("/v1/recall", response_model=Dict[str, Any])
async def recall_v1_endpoint(
    request: V1RecallRequest,
    principal: str = Depends(resolve_principal),
):
    """Offline V1 recall with record provenance and coverage boundaries.

    M5-04：证据可见范围在 SQL 层先行过滤，再排序与 LIMIT。
    """

    agent_id = assert_agent_id_compat(request.agent_id, principal)
    try:
        return recall_records(
            request.query,
            limit=request.limit,
            as_of=request.as_of,
            agent_id=agent_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=500, detail="V1 recall failed")


class ProjectedRecallRequest(BaseModel):
    query: str
    limit: int = 5
    agent_id: Optional[str] = None
    as_of: Optional[str] = None


@router.post("/v1/recall/projected", response_model=Dict[str, Any])
async def recall_projected_endpoint(
    request: ProjectedRecallRequest,
    principal: str = Depends(resolve_principal),
):
    """Evidence recall with Claim ownership and adjudication annotations.

    M5-04：memories 只含当前身份可见记录；跨页不可见证据的 Claim 以
    restricted 占位符脱敏返回。
    """
    agent_id = assert_agent_id_compat(request.agent_id, principal)
    try:
        return recall_with_projection(
            request.query,
            agent_id=agent_id,
            limit=request.limit,
            as_of=request.as_of,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        # Internal paths/schema details must not become an API response.
        raise HTTPException(status_code=500, detail="Projected recall failed")
