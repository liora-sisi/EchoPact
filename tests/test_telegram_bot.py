"""Synthetic-only acceptance for the M15 Telegram read-only loop."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package
from backend.telegram_bot.readonly_api import (
    ReadonlyApiConfig,
    create_readonly_app,
)
from backend.telegram_bot import runtime as bot_runtime
from backend.telegram_bot.runtime import (
    BadCaseStore,
    EchoTelegramBot,
    LocalRecallClient,
    TelegramBotApi,
)


OWNER = "agt-telegram-bot-owner"
ALLOWED = 778899


def _record(record_id: str, source_kind: str, content: str) -> dict:
    authority = (
        "codex-local-unverified-archive"
        if source_kind == "codex-app-server-v1"
        else "room-ferry-unverified-backup"
    )
    return {
        "record_id": record_id,
        "source_kind": source_kind,
        "source_ref": f"synthetic://telegram/{record_id}",
        "conversation_id": f"conversation-{source_kind}",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": "user",
        "content": content,
        "created_at": "2026-09-15T00:00:00Z",
        "verified": False,
        "authority": authority,
        "source_cutoff_at": "2026-09-16T00:00:00Z",
        "conflict_group_id": None,
    }


def _database(tmp_path: Path, records: list[dict]) -> Path:
    db_path = tmp_path / "telegram.sqlite3"
    package_path = tmp_path / "records.json"
    package_path.write_text(
        json.dumps(
            {"schema_version": "echo-pact-records-v1", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Telegram owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    return db_path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _update(text: str, *, user_id: int = ALLOWED, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
    }


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    def get_updates(self, *, offset):
        return []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class FakeRecall:
    def __init__(self):
        self.queries: list[str] = []

    def recall(self, query: str) -> dict:
        self.queries.append(query)
        return {
            "query": query,
            "recall_mode": "sqlite_fts5_trigram",
            "memories": [
                {
                    "record_id": "codex-a",
                    "content": "第一条 Codex 记忆",
                    "created_at": "2026-09-15T00:00:00Z",
                    "verified": False,
                },
                {
                    "record_id": "codex-b",
                    "content": "第二条 Codex 记忆",
                    "created_at": "2026-09-15T01:00:00Z",
                    "verified": False,
                },
            ],
            "recall_debug": {
                "top_k": [
                    {"record_id": "codex-a", "source_channel": "codex_local"},
                    {"record_id": "codex-b", "source_channel": "codex_local"},
                ]
            },
        }


class FakeAnswerer:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def answer(self, query: str, recall) -> str:
        if self.fail:
            raise RuntimeError("synthetic model failure")
        return "这是只根据两条 Codex 证据整理的回答。"


def _bot(tmp_path: Path, *, answerer=None):
    telegram = FakeTelegram()
    recall = FakeRecall()
    forbidden = tmp_path / "repo"
    bad_cases = BadCaseStore(
        str(tmp_path / "private" / "bad_cases"),
        forbidden_root=str(forbidden),
    )
    bot = EchoTelegramBot(
        allowed_user_id=ALLOWED,
        telegram=telegram,
        recall_client=recall,
        bad_cases=bad_cases,
        answerer=answerer,
    )
    return bot, telegram, recall, bad_cases


def test_outsider_and_non_private_chat_are_silently_rejected(tmp_path):
    bot, telegram, recall, _ = _bot(tmp_path)

    assert bot.handle_update(_update("能看到吗", user_id=123456)) == "ignored"
    group_update = _update("群里能看到吗")
    group_update["message"]["chat"] = {"id": -100123, "type": "group"}
    assert bot.handle_update(group_update) == "ignored"

    assert telegram.sent == []
    assert recall.queries == []


def test_allowed_text_gets_model_answer_and_model_failure_is_explicit(tmp_path):
    bot, telegram, recall, _ = _bot(tmp_path, answerer=FakeAnswerer())
    assert bot.handle_update(_update("我最近手腕怎么样")) == "answered"
    assert recall.queries == ["我最近手腕怎么样"]
    assert telegram.sent[-1][1].startswith("这是只根据两条 Codex 证据整理的回答。")
    assert "【召回说明】Codex库2条｜原问直接查" in telegram.sent[-1][1]

    failed_bot, failed_telegram, _, _ = _bot(
        tmp_path / "failed", answerer=FakeAnswerer(fail=True)
    )
    assert failed_bot.handle_update(_update("再问一次")) == "answered"
    assert "记忆召回了A「第一条 Codex 记忆」和B「第二条 Codex 记忆」" in (
        failed_telegram.sent[-1][1]
    )
    assert "嘴被捂住了" in failed_telegram.sent[-1][1]
    assert "Codex库2条" in failed_telegram.sent[-1][1]


def test_badcase_command_saves_last_dialogue_and_debug_packet(tmp_path):
    bot, telegram, _, bad_cases = _bot(tmp_path, answerer=FakeAnswerer())
    bot.handle_update(_update("这条为什么召回错了", update_id=10))

    assert bot.handle_update(
        _update("/badcase 应该优先找到手腕那条", update_id=11)
    ) == "bad_case_saved"

    files = list(bad_cases.root.glob("bad-case-*.json"))
    assert len(files) == 1
    packet = json.loads(files[0].read_text(encoding="utf-8"))
    assert packet["schema_version"] == "echo-pact-bad-case-v1"
    assert packet["note"] == "应该优先找到手腕那条"
    assert packet["dialogue"][0]["content"] == "这条为什么召回错了"
    assert packet["recall"]["recall_debug"]["top_k"][0][
        "source_channel"
    ] == "codex_local"
    assert "已经收进本地" in telegram.sent[-1][1]
    assert not list(bad_cases.root.glob("*.tmp"))

    bot.handle_update(_update("/badcase 重复点击不应再写一份", update_id=12))
    assert len(list(bad_cases.root.glob("bad-case-*.json"))) == 1


def test_badcase_store_refuses_repository_path(tmp_path):
    repository = tmp_path / "repo"
    with pytest.raises(ValueError, match="outside the repository"):
        BadCaseStore(
            str(repository / "bad_cases"),
            forbidden_root=str(repository),
        )


def test_loopback_api_accepts_codex_only_db_and_never_writes_it(tmp_path):
    db_path = _database(
        tmp_path,
        [_record("codex-only", "codex-app-server-v1", "M15-ONLY-8811 只读证据")],
    )
    before = _hash(db_path)
    app = create_readonly_app(
        ReadonlyApiConfig(
            db_path=str(db_path),
            agent_id=OWNER,
            result_limit=3,
            local_access_token="synthetic-local-token-123456789",
        )
    )

    with TestClient(app) as client:
        health = client.get("/health")
        denied = client.post("/recall-debug", json={"query": "M15-ONLY-8811"})
        recalled = client.post(
            "/recall-debug",
            json={"query": "M15-ONLY-8811"},
            headers={"X-Echo-Local-Token": "synthetic-local-token-123456789"},
        )

    assert health.status_code == 200
    assert health.json()["source_channels"] == ["codex_local"]
    assert denied.status_code == 401
    assert recalled.status_code == 200
    assert recalled.json()["recall_debug"]["top_k"][0][
        "source_channel"
    ] == "codex_local"
    assert _hash(db_path) == before


def test_loopback_api_rejects_mixed_database_for_b_only_phase(tmp_path):
    db_path = _database(
        tmp_path,
        [
            _record("codex", "codex-app-server-v1", "M15-MIX-5544 Codex"),
            _record("chatgpt", "room-ferry-backup-v1", "M15-MIX-5544 ChatGPT"),
        ],
    )

    with pytest.raises(RuntimeError, match="isolated source scope"):
        create_readonly_app(
            ReadonlyApiConfig(db_path=str(db_path), agent_id=OWNER)
        )


def test_local_recall_client_sends_ephemeral_loopback_token(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"memories": []}'

    def fake_urlopen(req, timeout):
        captured["token"] = req.get_header("X-echo-local-token")
        captured["url"] = req.full_url
        return Response()

    monkeypatch.setattr(bot_runtime.request, "urlopen", fake_urlopen)
    result = LocalRecallClient(
        "http://127.0.0.1:8765",
        access_token="ephemeral-loopback-token-123456",
    ).recall("测试问题")

    assert result == {"memories": []}
    assert captured == {
        "token": "ephemeral-loopback-token-123456",
        "url": "http://127.0.0.1:8765/recall-debug",
    }


def test_telegram_transport_error_does_not_retain_token_url(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic transport failure")

    monkeypatch.setattr(bot_runtime.request, "urlopen", fail)
    client = TelegramBotApi("secret-bot-token-should-not-escape")

    with pytest.raises(RuntimeError, match="Telegram Bot API request failed") as caught:
        client.get_updates(offset=None)

    assert "secret-bot-token" not in str(caught.value)
    assert caught.value.__cause__ is None
