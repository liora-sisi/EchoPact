"""Synthetic regression tests for natural-language SQLite recall planning."""

from __future__ import annotations

import json

import pytest

from backend.memory.identity import register_agent
from backend.memory.records_v1 import (
    MAX_LIKE_TERMS,
    MAX_RELAXED_TERMS_PER_GROUP,
    _recall_query_plan,
    import_record_package,
    recall_records,
)


OWNER = "agt-query-owner"
OUTSIDER = "agt-query-outsider"


def _record(record_id: str, content: str, created_at: str) -> dict:
    return {
        "record_id": record_id,
        "source_kind": "synthetic_conversation",
        "source_ref": f"synthetic://recall-query/{record_id}",
        "conversation_id": "synthetic-recall-query",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": "user",
        "content": content,
        "created_at": created_at,
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-08-01T00:00:00Z",
        "conflict_group_id": None,
    }


@pytest.fixture
def query_db(tmp_path):
    db_path = tmp_path / "recall-query.sqlite3"
    package_path = tmp_path / "records.json"
    records = [
        _record(
            "cats",
            "豆豆和贝贝是家里的两只猫。",
            "2026-07-01T00:00:00Z",
        ),
        _record(
            "night-shift",
            "下完夜班以后先睡觉休息。",
            "2026-07-02T00:00:00Z",
        ),
        _record(
            "echo-pact",
            "Echo Pact 是来源无关的外置记忆基础设施。",
            "2026-07-03T00:00:00Z",
        ),
        _record(
            "deepseek",
            "DeepSeek 写下了早期框架。",
            "2026-07-04T00:00:00Z",
        ),
        _record(
            "echo-only",
            "Echo 是一个单独出现的英文词。",
            "2026-07-05T00:00:00Z",
        ),
        _record(
            "pact-only",
            "Pact 也是一个单独出现的英文词。",
            "2026-07-06T00:00:00Z",
        ),
        _record(
            "deep-only",
            "Deep 只用于制造宽泛匹配干扰。",
            "2026-07-07T00:00:00Z",
        ),
        _record(
            "seek-only",
            "Seek 只用于制造宽泛匹配干扰。",
            "2026-07-08T00:00:00Z",
        ),
        _record(
            "name-only",
            "给测试文件取名字时保持确定性。",
            "2026-07-09T00:00:00Z",
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": "echo-pact-records-v1", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Synthetic query owner", actor="test", db_path=str(db_path))
    register_agent(
        OUTSIDER, "Synthetic query outsider", actor="test", db_path=str(db_path)
    )
    summary = import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    assert summary["added"] == len(records)
    return db_path


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("豆豆 贝贝", "cats"),
        ("家里的猫叫什么名字", "cats"),
        ("我下夜班以后很困", "night-shift"),
    ],
)
def test_natural_chinese_queries_recall_expected_record(query_db, query, expected_id):
    response = recall_records(query, limit=3, db_path=str(query_db))

    assert response["memories"]
    assert response["memories"][0]["record_id"] == expected_id
    assert response["recall_mode"] in {
        "sqlite_fts5_trigram_focused",
        "sqlite_fts5_trigram_relaxed",
        "sqlite_like_terms_focused",
        "sqlite_like_terms_fallback",
    }


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [("Echo Pact", "echo-pact"), ("Deep Seek", "deepseek")],
)
def test_spaced_english_names_prefer_phrase_or_compact_alias(
    query_db, query, expected_id
):
    response = recall_records(query, limit=5, db_path=str(query_db))

    assert [item["record_id"] for item in response["memories"]] == [expected_id]
    assert response["recall_mode"] == "sqlite_fts5_trigram"


def test_exact_query_keeps_existing_fast_path(query_db):
    response = recall_records("外置记忆", limit=5, db_path=str(query_db))

    assert [item["record_id"] for item in response["memories"]] == ["echo-pact"]
    assert response["recall_mode"] == "sqlite_fts5_trigram"


def test_unknown_anchor_does_not_return_relaxed_noise(query_db):
    response = recall_records(
        "NEVER-EXISTED-ECHO-PACT-9F3A", limit=5, db_path=str(query_db)
    )

    assert response["memories"] == []


def test_query_plan_bounds_long_untrusted_input():
    cjk_plan = _recall_query_plan("甲乙丙丁戊己庚辛壬癸" * 200)
    short_plan = _recall_query_plan(" ".join(f"甲{index % 10}" for index in range(200)))

    assert (
        cjk_plan.relaxed_expression.count('"') // 2
        <= MAX_RELAXED_TERMS_PER_GROUP
    )
    assert len(short_plan.like_terms) <= MAX_LIKE_TERMS


def test_focused_like_tier_keeps_agent_visibility_inside_sql(query_db):
    owner = recall_records(
        "家里的猫叫什么名字",
        limit=5,
        db_path=str(query_db),
        agent_id=OWNER,
        read_only=True,
    )
    outsider = recall_records(
        "家里的猫叫什么名字",
        limit=5,
        db_path=str(query_db),
        agent_id=OUTSIDER,
        read_only=True,
    )

    assert [item["record_id"] for item in owner["memories"]] == ["cats"]
    assert outsider["memories"] == []
    assert outsider["coverage"]["coverage_status"] == "no_visible_records"
