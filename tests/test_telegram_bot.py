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
    OpenAICompatibleAnswerer,
    TelegramBotApi,
)


OWNER = "agt-telegram-bot-owner"
ALLOWED = 778899


def test_mobile_source_label_distinguishes_browser_capture():
    assert bot_runtime._detailed_source_label(
        {
            "source_channel": "chatgpt",
            "source_kind": "guijian-browser-capture-v1",
        }
    ) == "GPT库（浏览器抓取）"


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
            "coverage": {
                "verified_knowledge_cutoff_at": "2026-10-28T01:32:19Z",
                "coverage_gap": False,
            },
            "record_time_filter": {
                "applied": True,
                "created_at_start": "2026-09-14T16:00:00Z",
                "created_at_end_exclusive": "2026-09-15T16:00:00Z",
            },
            "temporal_scope": {
                "status": "resolved_calendar_scope",
                "matched_expression": "9月15日",
                "resolved_on": "2026-09-15",
                "filter_start_at": "2026-09-14T16:00:00Z",
                "filter_end_at_exclusive": "2026-09-15T16:00:00Z",
            },
            "event_timeline": {
                "archive_first_scope": {
                    "kind": "bounded_recalled_evidence",
                    "exhaustive": False,
                    "claim": "earliest observed in this packet, not an absolute archive first",
                }
            },
            "memories": [
                {
                    "record_id": "chatgpt-a",
                    "content": "第一条 GPT 记忆",
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
                    {
                        "position": 1,
                        "record_id": "chatgpt-a",
                        "source_channel": "chatgpt",
                        "confidence": 0.91,
                        "confidence_basis": ["合成词面命中"],
                        "adaptive_match_passes": ["initial"],
                        "retrieval_debug": {
                            "position_in_pass": 1,
                            "lexical_rank": -2.0,
                            "rank_semantics": "synthetic_lexical_rank",
                        },
                    },
                    {
                        "position": 2,
                        "record_id": "codex-b",
                        "source_channel": "codex_local",
                        "confidence": 0.82,
                        "confidence_basis": ["合成意图命中"],
                        "adaptive_match_passes": ["intent_expansion"],
                        "retrieval_debug": {
                            "position_in_pass": 2,
                            "lexical_rank": -1.0,
                            "rank_semantics": "synthetic_lexical_rank",
                        },
                    },
                ],
                "passes": [
                    {
                        "pass": "initial",
                        "query": query,
                        "recall_mode": "sqlite_fts5_trigram",
                        "candidate_count": 2,
                    }
                ],
                "exclusions": [
                    {
                        "record_id": "outside-c",
                        "source_channel": "chatgpt",
                        "reason_code": "result_limit",
                        "reason": "候选进入了召回池，但排在本次 Top-K 之外",
                        "content_preview": "第三条未进入 Top-K 的合成记录",
                    }
                ],
            },
        }


class FakeAnswerer:
    def __init__(self, *, fail: bool = False, max_evidence_items: int = 1):
        self.fail = fail
        self.max_evidence_items = max_evidence_items

    def answer(self, query: str, recall) -> str:
        if self.fail:
            raise RuntimeError("synthetic model failure")
        return "这是只根据 GPT 与 Codex 两条证据整理的回答。"


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
    assert telegram.sent[-1][1].startswith(
        "这是只根据 GPT 与 Codex 两条证据整理的回答。"
    )
    assert "【召回说明】GPT库1条、Codex库1条｜原问直接查" in (
        telegram.sent[-1][1]
    )

    failed_bot, failed_telegram, _, _ = _bot(
        tmp_path / "failed", answerer=FakeAnswerer(fail=True)
    )
    assert failed_bot.handle_update(_update("再问一次")) == "answered"
    assert "记忆召回了A「第一条 GPT 记忆」和B「第二条 Codex 记忆」" in (
        failed_telegram.sent[-1][1]
    )
    assert "嘴被捂住了" in failed_telegram.sent[-1][1]
    assert "GPT库1条、Codex库1条" in failed_telegram.sent[-1][1]


def test_why_command_explains_exact_previous_recall_and_model_boundary(tmp_path):
    bot, telegram, recall, _ = _bot(tmp_path, answerer=FakeAnswerer())

    assert bot.handle_update(_update("Echo 为什么这样答？", update_id=20)) == "answered"
    prior_message_count = len(telegram.sent)
    assert bot.handle_update(_update("/why", update_id=21)) == "why_explained"

    messages = telegram.sent[prior_message_count:]
    rendered = "\n".join(message for _, message in messages)
    assert recall.queries == ["Echo 为什么这样答？"]
    assert "前 1 条进入脑袋输入" in rendered
    assert "第 1 条｜GPT库｜脑袋看到" in rendered
    assert "第 2 条｜Codex库｜未送脑袋" in rendered
    assert "召回理由：合成词面命中" in rendered
    assert "脑袋实际看到的原文：\n第一条 GPT 记忆" in rendered
    assert "Echo 召回的原文：\n第二条 Codex 记忆" in rendered
    assert "候选进入了召回池，但排在本次 Top-K 之外" in rendered
    assert "日期解释：9月15日 → 2026-09-15（成都）" in rendered
    assert "最早口径：只是在本次证据包里最早" in rendered
    assert "日期覆盖：请求的整段日期位于当前已验证数据截止线内" in rendered
    assert all(len(message) <= bot_runtime.TELEGRAM_MESSAGE_LIMIT for _, message in messages)


def test_why_command_requires_a_previous_question(tmp_path):
    bot, telegram, recall, _ = _bot(tmp_path, answerer=FakeAnswerer())

    assert bot.handle_update(_update("/why")) == "no_why_context"
    assert "先问我一条记忆问题" in telegram.sent[-1][1]
    assert recall.queries == []


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
    assert packet["model_evidence_limit"] == 1
    assert packet["dialogue"][0]["content"] == "这条为什么召回错了"
    assert packet["recall"]["recall_debug"]["top_k"][0][
        "source_channel"
    ] == "chatgpt"
    assert packet["recall"]["temporal_scope"]["resolved_on"] == "2026-09-15"
    assert packet["recall"]["record_time_filter"]["applied"] is True
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


def test_loopback_api_can_accept_explicit_codex_only_scope_and_never_writes_it(
    tmp_path,
):
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
            expected_source_channels=("codex_local",),
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


def test_loopback_api_accepts_required_gpt_and_codex_sources(tmp_path):
    db_path = _database(
        tmp_path,
        [
            _record("codex", "codex-app-server-v1", "M15-MIX-5544 Codex"),
            _record("chatgpt", "room-ferry-backup-v1", "M15-MIX-5544 ChatGPT"),
        ],
    )

    app = create_readonly_app(
        ReadonlyApiConfig(db_path=str(db_path), agent_id=OWNER)
    )

    with TestClient(app) as client:
        health = client.get("/health")

    assert health.status_code == 200
    assert health.json()["source_channels"] == ["chatgpt", "codex_local"]


def test_loopback_api_rejects_codex_only_db_when_both_sources_are_required(
    tmp_path,
):
    db_path = _database(
        tmp_path,
        [_record("codex", "codex-app-server-v1", "M15-SCOPE-6601 Codex")],
    )

    with pytest.raises(RuntimeError, match="required source scope"):
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


def test_answerer_bounds_evidence_and_can_disable_thinking(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "自然整理后的回答"}}]}
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr(bot_runtime.request, "urlopen", fake_urlopen)
    long_evidence = "甲" * 1200 + "不应发送"
    answerer = OpenAICompatibleAnswerer(
        api_key="synthetic-deepseek-key",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        thinking_mode="disabled",
        max_tokens=600,
    )

    answer = answerer.answer(
        "我们以前怎么做手账？",
        {
            "memories": [
                {
                    "record_id": "gpt-a",
                    "content": long_evidence,
                    "created_at": "2026-09-01T00:00:00Z",
                    "verified": False,
                }
            ],
            "recall_debug": {
                "top_k": [{"record_id": "gpt-a", "source_channel": "chatgpt"}]
            },
        },
    )

    assert answer == "自然整理后的回答"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    payload = captured["payload"]
    assert payload["model"] == "deepseek-flash"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 600
    assert "甲" * 1200 in payload["messages"][1]["content"]
    assert "不应发送" not in payload["messages"][1]["content"]


def test_answerer_limits_external_evidence_items(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "合成回答"}}]}
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr(bot_runtime.request, "urlopen", fake_urlopen)
    answerer = OpenAICompatibleAnswerer(
        api_key="synthetic-deepseek-key",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        max_evidence_items=2,
    )

    answerer.answer(
        "哪两条有关？",
        {
            "memories": [
                {"record_id": "a", "content": "第一条"},
                {"record_id": "b", "content": "第二条"},
                {"record_id": "c", "content": "不应外送的第三条"},
            ],
            "temporal_scope": {
                "status": "resolved_calendar_scope",
                "matched_expression": "9月15日",
                "resolved_on": "2026-09-15",
                "filter_start_at": "2026-09-14T16:00:00Z",
                "filter_end_at_exclusive": "2026-09-15T16:00:00Z",
            },
            "coverage": {
                "verified_knowledge_cutoff_at": "2026-10-28T01:32:19Z",
                "coverage_gap": True,
            },
            "event_timeline": {
                "archive_first_scope": {
                    "kind": "bounded_recalled_evidence",
                    "exhaustive": False,
                    "claim": "bounded only",
                }
            },
        },
    )

    evidence = captured["payload"]["messages"][1]["content"]
    assert "第一条" in evidence
    assert "第二条" in evidence
    assert "不应外送的第三条" not in evidence
    assert "explicit user corrections" in captured["payload"]["messages"][0][
        "content"
    ]
    assert "calendar_scope=" in captured["payload"]["messages"][1]["content"]
    assert "earliest_scope=" in captured["payload"]["messages"][1]["content"]
    assert "requested_scope=within_verified_cutoff" in captured["payload"][
        "messages"
    ][1]["content"]
