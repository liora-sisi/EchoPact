# 契约驱动测试（API契约表草案 2026-05-29 · 近期优先级 #4）
# 目标：把 /recall 的输入输出字段结构钉死，防止前端接入时字段漂移。
import pytest
from fastapi.testclient import TestClient

import backend.utils.db as db_module
from backend.utils.db import init_db
from backend.memory.models import Memory
from backend.memory.crud import create_memory
from backend.trigger.main import app

CONTRACT_REASON_FIELDS = {
    "semantic_score", "emotion_weight", "importance",
    "decay", "saga_boost", "undone_bonus", "recall_reason",
}


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_module.DB_PATH = str(tmp_path / "contract.db")
    init_db()
    monkeypatch.setattr(
        "backend.memory.vector_store.search_similar",
        lambda query, limit, agent_id: []
    )


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_recall_contract_fields(client):
    create_memory(Memory(content="保长催我写master", valence=0.3,
                         arousal=0.8, agent_id="contract"))
    resp = client.post("/api/recall", json={
        "query": "催更", "limit": 5, "agent_id": "contract",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "memories" in body
    assert len(body["memories"]) == 1

    item = body["memories"][0]
    for key in ("id", "content", "weight", "reason"):
        assert key in item, f"items[] 缺少契约字段 {key}"
    assert CONTRACT_REASON_FIELDS <= set(item["reason"].keys()), (
        f"reason 缺少契约字段: {CONTRACT_REASON_FIELDS - set(item['reason'].keys())}"
    )


def test_recall_reason_not_all_fallback(client):
    # 验收标准：recall_reason 不应全部回退成"暂无温度"类兜底文案
    create_memory(Memory(content="保长骂完又夸克里及格", valence=0.5,
                         arousal=0.8, agent_id="contract"))
    resp = client.post("/api/recall", json={
        "query": "保长", "agent_id": "contract",
    })
    reason = resp.json()["memories"][0]["reason"]["recall_reason"]
    assert "保长" in reason
    assert "暂无" not in reason and "还没读出" not in reason


def test_recall_agent_isolation_via_api(client):
    create_memory(Memory(content="克里的记忆", agent_id="keli"))
    create_memory(Memory(content="保长的记忆", agent_id="baozhang"))
    resp = client.post("/api/recall", json={"query": "记忆", "agent_id": "keli"})
    contents = [m["content"] for m in resp.json()["memories"]]
    assert contents == ["克里的记忆"]


def test_recall_failure_returns_detail(client):
    # 契约：失败时也要有明确字段（FastAPI 校验错误返回 detail）
    resp = client.post("/api/recall", json={"limit": 5})
    assert resp.status_code == 422
    assert "detail" in resp.json()
