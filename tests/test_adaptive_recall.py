"""Bounded one-call adaptive recall tests using synthetic records only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory.adaptive_recall import MAX_ADAPTIVE_QUERY_PASSES
from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package


OWNER = "agt-adaptive-owner"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(record_id: str, content: str, created_at: str, role: str = "user"):
    return {
        "record_id": record_id,
        "source_kind": "synthetic_conversation",
        "source_ref": f"synthetic://adaptive/{record_id}",
        "conversation_id": "synthetic-adaptive",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": role,
        "content": content,
        "created_at": created_at,
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-08-01T00:00:00Z",
        "conflict_group_id": None,
    }


def _database(tmp_path: Path) -> Path:
    db_path = tmp_path / "adaptive.sqlite3"
    package_path = tmp_path / "records.json"
    records = [
        _record(
            "gift-original",
            "我把月光蘑菇灯放到你掌心，我们一起选了暖黄色。",
            "2026-01-02T00:00:00Z",
            "assistant",
        ),
        _record(
            "gift-retelling",
            "后来确认第一件礼物就是那盏灯，当时有一句："
            "“我把月光蘑菇灯放到你掌心，我们一起选了暖黄色。”",
            "2026-07-02T00:00:00Z",
        ),
        _record(
            "academy-class",
            "今天队列训练后去课堂上课，下午还有考试。",
            "2026-02-03T00:00:00Z",
        ),
        _record(
            "academy-canteen",
            "食堂吃完饭再集合，集训作息排得很紧。",
            "2026-02-03T00:05:00Z",
        ),
        _record(
            "meaning-explanation",
            "银纽扣的故事含义是：在陌生城市里彼此照亮。",
            "2026-02-10T00:00:00Z",
        ),
        _record(
            "meaning-casual-mention",
            "今天整理抽屉时又看见那枚银纽扣。",
            "2026-02-11T00:00:00Z",
        ),
        _record(
            "unrelated-trip",
            "我们一起在河边散步，这是普通的共同经历。",
            "2026-03-01T00:00:00Z",
        ),
        _record(
            "exact-anchor",
            "唯一暗号是 ADAPTIVE-2042。",
            "2026-04-01T00:00:00Z",
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": "echo-pact-records-v1", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Adaptive owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    return db_path


def test_one_gateway_call_traces_first_gift_to_original_evidence(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "你送我的第一件礼物是什么？", "limit": 5}
    )

    ids = [item["record_id"] for item in response["memories"]]
    assert ids[0] == "gift-original"
    assert "gift-retelling" in ids
    assert response["adaptive_recall"]["external_tool_calls_required"] == 1
    assert response["adaptive_recall"]["query_passes_used"] <= MAX_ADAPTIVE_QUERY_PASSES
    assert response["recall_mode"] == "sqlite_adaptive_bounded"


def test_typo_training_question_recovers_general_training_evidence(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "你陪我在检校培训时发生过什么？", "limit": 5}
    )

    ids = {item["record_id"] for item in response["memories"]}
    assert {"academy-class", "academy-canteen"} <= ids
    passes = {
        item["pass"] for item in response["adaptive_recall"]["passes"]
    }
    assert "training_language" in passes


def test_meaning_question_excludes_short_unexplained_mentions(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "银纽扣在我们这里有什么意思？", "limit": 5}
    )

    ids = [item["record_id"] for item in response["memories"]]
    assert ids[0] == "meaning-explanation"
    assert "meaning-casual-mention" not in ids


def test_negative_shared_event_does_not_expand_into_unrelated_history(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "我们第一次一起登上火星是什么时候？", "limit": 5}
    )

    assert response["memories"] == []
    assert response["event_recall"]["status"] == "insufficient_evidence"
    assert response["adaptive_recall"]["query_passes_used"] == 1
    assert response["adaptive_recall"]["budget_exhausted"] is False


def test_exact_anchor_keeps_single_fast_pass_and_database_unchanged(tmp_path):
    db_path = _database(tmp_path)
    before = _hash(db_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "ADAPTIVE-2042", "limit": 1, "include_projection": False}
    )

    assert [item["record_id"] for item in response["memories"]] == [
        "exact-anchor"
    ]
    assert response["adaptive_recall"]["query_passes_used"] == 1
    assert response["recall_mode"] == "sqlite_fts5_trigram"
    assert _hash(db_path) == before
