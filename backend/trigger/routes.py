from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any
from ..judge.recall import recall_memories

router = APIRouter()

class RecallRequest(BaseModel):
    query: str
    limit: int = 5
    agent_id: str = "default"

@router.post("/recall", response_model=Dict[str, List[Dict[str, Any]]])
async def recall_endpoint(request: RecallRequest):
    try:
        results = recall_memories(request.query, request.limit, agent_id=request.agent_id)
        return {"memories": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
