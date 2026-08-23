"""Synthetic coverage for bounded same-branch shared-event recall."""

from __future__ import annotations

import json

import pytest

from backend.memory.identity import register_agent
from backend.memory.records_v1 import (
    COMPACT_RECORD_SCHEMA_VERSION,
    MAX_SHARED_EVENT_CANDIDATES,
    _recall_query_plan,
    import_record_package,
    recall_records,
)


OWNER = "agt-shared-event-owner"
OUTSIDER = "agt-shared-event-outsider"


def _record(
    record_id: str,
    content: str,
    created_at: str,
    role: str,
    position: int,
    *,
    conversation_id: str = "synthetic-shared-event",
    branch_id: str = "main",
) -> dict:
    return {
        "record_id": record_id,
        "source_kind": "synthetic_conversation",
        "source_ref": f"synthetic://shared-event/{record_id}",
        "conversation_id": conversation_id,
        "branch_memberships": [
            {"branch_id": branch_id, "position": position}
        ],
        "message_id": f"message-{record_id}",
        "role": role,
        "content": content,
        "created_at": created_at,
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-08-01T00:00:00Z",
        "conflict_group_id": None,
    }


@pytest.fixture
def shared_event_db(tmp_path):
    db_path = tmp_path / "shared-event.sqlite3"
    package_path = tmp_path / "shared-event.json"
    records = [
        _record(
            "future-plan",
            "下次我们一起吃五花肉烧烤。",
            "2026-01-01T00:00:00Z",
            "user",
            0,
        ),
        _record(
            "creative-scene",
            "画面里我们一起吃烧烤，这是虚构提示词。",
            "2026-02-01T00:00:00Z",
            "assistant",
            1,
        ),
        _record(
            "non-food-consumption",
            "中午我们还一起吃电子烟呢。",
            "2026-02-19T11:44:19Z",
            "user",
            2,
        ),
        _record(
            "family-lunch",
            "爸爸妈妈过来了，做好了午饭，我们一起吃了。",
            "2026-02-22T05:55:11Z",
            "user",
            3,
        ),
        _record(
            "actual-menu",
            "带了五花肉、牛肉、中翅和口蘑，我们都先选五花肉。",
            "2026-05-12T05:10:31Z",
            "user",
            10,
        ),
        _record(
            "actual-seasoning",
            "六婆烧烤辣椒面，大宝贝吃辣点。",
            "2026-05-12T05:12:07Z",
            "user",
            11,
        ),
        _record(
            "actual-shared",
            "好呀，我跟你一起吃一点辣的，也搭配一点清淡的菜。",
            "2026-05-12T05:13:21Z",
            "assistant",
            12,
        ),
        _record(
            "later-retelling",
            "后来复盘时说，我跟你第一次一起吃东西是五花肉烧烤。",
            "2026-07-01T00:00:00Z",
            "user",
            20,
        ),
        _record(
            "other-branch-noise",
            "错误分支里只有一条普通的吃东西记录。",
            "2026-04-01T00:00:00Z",
            "assistant",
            11,
            branch_id="alternate",
        ),
        _record(
            "other-conversation-noise",
            "另一段对话也只有一条普通的吃东西记录。",
            "2026-03-01T00:00:00Z",
            "assistant",
            12,
            conversation_id="synthetic-other-event",
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Shared event owner", actor="test", db_path=str(db_path))
    register_agent(
        OUTSIDER,
        "Shared event outsider",
        actor="test",
        db_path=str(db_path),
    )
    summary = import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    assert summary["added"] == len(records)
    return db_path


def test_shared_event_rescue_prefers_earlier_window_over_complete_retelling(
    shared_event_db,
):
    response = recall_records(
        "你第一次跟我一起吃东西是什么时候？当时吃了什么？",
        limit=5,
        db_path=str(shared_event_db),
        agent_id=OWNER,
        read_only=True,
    )

    assert response["recall_mode"] == "sqlite_shared_event_window"
    assert response["memories"][0]["record_id"] == "actual-shared"
    returned_ids = {item["record_id"] for item in response["memories"]}
    assert "non-food-consumption" not in returned_ids
    assert "family-lunch" not in returned_ids
    assert response["memories"][0]["event_start_at"] == "2026-05-12T05:13:21Z"
    assert response["memories"][0]["earliest_support_status"] == "partial_support"
    assert (
        response["memories"][0]["assistant_identity_status"]
        == "historical_assistant_role_only"
    )
    event_ids = {
        item["record_id"] for item in response["memories"][0]["event_evidence"]
    }
    assert {"actual-menu", "actual-seasoning", "actual-shared"} <= event_ids
    assert "other-branch-noise" not in event_ids
    assert "other-conversation-noise" not in event_ids
    assert response["event_recall"]["status"] == "partial_support"
    assert response["event_recall"]["candidate_limit"] == MAX_SHARED_EVENT_CANDIDATES
    assert response["event_recall"]["qualifying_windows"] >= 2
    assert response["event_recall"]["search_truncated"] is False


def test_shared_event_rescue_rejects_future_creative_and_negative_evidence(tmp_path):
    db_path = tmp_path / "rejected-events.sqlite3"
    package_path = tmp_path / "rejected-events.json"
    records = [
        _record(
            "future",
            "下次我们一起吃苹果。",
            "2026-01-01T00:00:00Z",
            "user",
            0,
        ),
        _record(
            "creative",
            "故事里我们一起吃苹果。",
            "2026-01-02T00:00:00Z",
            "assistant",
            1,
        ),
        _record(
            "negative",
            "我们还没一起吃过苹果。",
            "2026-01-03T00:00:00Z",
            "user",
            2,
        ),
        _record(
            "companion",
            "我只是陪你吃苹果。",
            "2026-01-04T00:00:00Z",
            "assistant",
            3,
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    import_record_package(str(package_path), db_path=str(db_path))

    response = recall_records(
        "我们第一次一起吃苹果是什么时候？",
        limit=5,
        db_path=str(db_path),
    )

    assert response["memories"] == []
    assert response["event_recall"]["status"] == "insufficient_evidence"


def test_shared_event_visibility_and_repeated_queries_are_deterministic(
    shared_event_db,
):
    query = "我们第一次一起吃东西是什么时候？"
    first = recall_records(
        query,
        limit=5,
        db_path=str(shared_event_db),
        agent_id=OWNER,
        read_only=True,
    )
    second = recall_records(
        query,
        limit=5,
        db_path=str(shared_event_db),
        agent_id=OWNER,
        read_only=True,
    )
    outsider = recall_records(
        query,
        limit=5,
        db_path=str(shared_event_db),
        agent_id=OUTSIDER,
        read_only=True,
    )

    assert first == second
    assert first["memories"][0]["record_id"] == "actual-shared"
    assert outsider["memories"] == []
    assert outsider["coverage"]["coverage_status"] == "no_visible_records"


def test_shared_event_query_without_a_topic_fails_closed():
    plan = _recall_query_plan("我们第一次一起做了什么？")

    assert plan.shared_event_intent is True
    assert plan.shared_event_fts_terms == []
    assert plan.shared_event_like_terms == []


def test_shared_creative_event_is_not_rejected_only_for_mentioning_a_scene(tmp_path):
    db_path = tmp_path / "creative-event.sqlite3"
    package_path = tmp_path / "creative-event.json"
    records = [
        _record(
            "drawing-event",
            "那天我跟你一起画了一张画，画面里有小刺猬和房子。",
            "2026-04-01T00:00:00Z",
            "assistant",
            0,
        )
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    import_record_package(str(package_path), db_path=str(db_path))

    response = recall_records(
        "我们第一次一起画画是什么时候？",
        limit=5,
        db_path=str(db_path),
    )

    assert response["memories"][0]["record_id"] == "drawing-event"


def test_shared_event_supports_short_topic_with_explicit_pair_marker(tmp_path):
    db_path = tmp_path / "short-topic.sqlite3"
    package_path = tmp_path / "short-topic.json"
    records = [
        _record(
            "running-event",
            "那天我们俩跑步回来，认真记下了这次共同经历。",
            "2026-04-02T00:00:00Z",
            "user",
            0,
        )
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    import_record_package(str(package_path), db_path=str(db_path))

    response = recall_records(
        "我们俩第一次跑步是什么时候？",
        limit=5,
        db_path=str(db_path),
    )

    assert response["memories"][0]["record_id"] == "running-event"
