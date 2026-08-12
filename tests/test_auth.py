"""M4.5 第二批：trigger 服务 fail-closed 鉴权测试。

覆盖工单：
- 未配置/空/占位访问码 → 503；
- 根健康检查保持 200；
- 缺失/错误 Bearer → 401 + WWW-Authenticate: Bearer；
- 正确 Bearer → /api/recall 与 /api/v1/recall 均可进入；
- 访问码不出现在响应与日志中；
- 全程不联网（TestClient 为进程内 ASGI 调用）。
"""
import logging

import pytest
from fastapi.testclient import TestClient

import backend.utils.db as db_module
import backend.trigger.routes as routes_module

VALID_CODE = "test-access-code-7f3a9c1e"  # 仅测试用合成串，非真实访问码
PLACEHOLDERS = ["", "your_access_code_here", "changeme", "placeholder"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_module.DB_PATH = str(tmp_path / "auth_test.db")
    from backend.trigger.main import app

    with TestClient(app) as c:
        yield c


def _recall_payloads():
    return ({"query": "保长", "limit": 3}, {"query": "保长", "limit": 3})


# ---------- 503：未配置 / 空 / 占位 ----------

@pytest.mark.parametrize("code", PLACEHOLDERS)
def test_unconfigured_or_placeholder_returns_503(client, monkeypatch, code):
    monkeypatch.setenv("ACCESS_CODE", code)
    for path in ("/api/recall", "/api/v1/recall", "/api/v1/recall/projected"):
        resp = client.post(path, json={"query": "保长", "limit": 3})
        assert resp.status_code == 503, f"{path} should be 503, got {resp.status_code}"


def test_unset_access_code_returns_503(client, monkeypatch):
    monkeypatch.delenv("ACCESS_CODE", raising=False)
    resp = client.post("/api/recall", json={"query": "保长", "limit": 3})
    assert resp.status_code == 503


# ---------- 根健康检查 ----------

def test_root_health_200_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("ACCESS_CODE", raising=False)
    resp = client.get("/")
    assert resp.status_code == 200


def test_root_health_200_when_configured(client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    resp = client.get("/")
    assert resp.status_code == 200


# ---------- 401：缺失 / 错误 Bearer ----------

def test_missing_bearer_returns_401_with_header(client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    for path in ("/api/recall", "/api/v1/recall", "/api/v1/recall/projected"):
        resp = client.post(path, json={"query": "保长", "limit": 3})
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_wrong_bearer_returns_401_with_header(client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    resp = client.post(
        "/api/recall",
        json={"query": "保长", "limit": 3},
        headers={"Authorization": "Bearer wrong-code-000"},
    )
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


# ---------- 200：正确 Bearer ----------

def test_correct_bearer_old_recall_ok(client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    resp = client.post(
        "/api/recall",
        json={"query": "保长", "limit": 3, "agent_id": "default"},
        headers={"Authorization": f"Bearer {VALID_CODE}"},
    )
    assert resp.status_code == 200
    assert "memories" in resp.json()


def test_correct_bearer_v1_recall_ok(client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    resp = client.post(
        "/api/v1/recall",
        json={"query": "保长", "limit": 3},
        headers={"Authorization": f"Bearer {VALID_CODE}"},
    )
    assert resp.status_code == 200


def test_correct_bearer_projected_recall_ok(client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    resp = client.post(
        "/api/v1/recall/projected",
        json={"query": "保长", "limit": 3, "agent_id": "default"},
        headers={"Authorization": f"Bearer {VALID_CODE}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == "echo-pact-recall-projection-v1"
    # M5-04：响应里的 agent_id 是认证派生身份；body 的 "default" 只是别名断言
    assert body["agent_id"] == "agt-legacy"
    assert "memories" in body


def test_projected_recall_internal_error_does_not_leak_details(client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    private_detail = r"D:\private\memory.db synthetic failure"

    def fail_join(*args, **kwargs):
        raise RuntimeError(private_detail)

    monkeypatch.setattr(routes_module, "recall_with_projection", fail_join)
    resp = client.post(
        "/api/v1/recall/projected",
        json={"query": "保长", "agent_id": "default"},
        headers={"Authorization": f"Bearer {VALID_CODE}"},
    )
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Projected recall failed"}
    assert private_detail not in resp.text


def test_projected_recall_rejects_empty_agent(client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    resp = client.post(
        "/api/v1/recall/projected",
        json={"query": "保长", "agent_id": "   "},
        headers={"Authorization": f"Bearer {VALID_CODE}"},
    )
    assert resp.status_code == 422


# ---------- 访问码不泄露 ----------

def test_access_code_not_in_responses(client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    responses = [
        client.post("/api/recall", json={"query": "保长"}),
        client.post("/api/recall", json={"query": "保长"},
                    headers={"Authorization": "Bearer wrong"}),
        client.post("/api/recall", json={"query": "保长"},
                    headers={"Authorization": f"Bearer {VALID_CODE}"}),
    ]
    for resp in responses:
        assert VALID_CODE not in resp.text


def test_access_code_not_in_logs(client, monkeypatch, caplog):
    monkeypatch.setenv("ACCESS_CODE", VALID_CODE)
    with caplog.at_level(logging.DEBUG):
        client.post("/api/recall", json={"query": "保长"},
                    headers={"Authorization": f"Bearer {VALID_CODE}"})
        client.post("/api/recall", json={"query": "保长"})
    assert VALID_CODE not in caplog.text
