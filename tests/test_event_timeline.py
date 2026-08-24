"""Synthetic tests for the query-time event and retelling timeline."""

from __future__ import annotations

import json
from pathlib import Path

from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory.event_timeline import (
    EVENT_TIMELINE_SCHEMA_VERSION,
    MAX_ORDINARY_TIMELINE_NODES,
    build_event_timeline,
    classify_mention_types,
)
from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package


OWNER = "agt-event-timeline-owner"


def _record(
    record_id: str,
    content: str,
    created_at: str,
    *,
    conflict_group_id=None,
):
    return {
        "record_id": record_id,
        "source_kind": "synthetic_conversation",
        "source_ref": f"synthetic://event-timeline/{record_id}",
        "conversation_id": "synthetic-event-timeline",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": "user",
        "content": content,
        "created_at": created_at,
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-08-24T00:00:00Z",
        "conflict_group_id": conflict_group_id,
    }


def _database(tmp_path: Path) -> Path:
    db_path = tmp_path / "event-timeline.sqlite3"
    package_path = tmp_path / "event-timeline.json"
    records = [
        _record(
            "picnic-first",
            "星桥晚餐发生在2026年6月1日，我跟你在桥边吃了一顿晚餐。",
            "2026-06-01T00:30:00+08:00",
        ),
        _record(
            "picnic-retelling",
            "后来回忆星桥晚餐时，我保留了原话：“那天风很轻。”",
            "2026-07-02T10:00:00+08:00",
        ),
        _record(
            "picnic-clock",
            "后来补充一个细节：星桥晚餐发生在"
            "2026-06-01T08:45:00+08:00。",
            "2026-07-03T10:00:00+08:00",
        ),
        _record(
            "picnic-correction",
            "更正：星桥晚餐不是在河西岸，这是一次纠正。",
            "2026-07-04T10:00:00+08:00",
            conflict_group_id="conflict-picnic-place",
        ),
        _record(
            "unrelated-retelling-noise",
            "后来复盘另一场无关聚会时，只谈了山顶看云。",
            "2026-07-05T10:00:00+08:00",
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": "echo-pact-records-v1", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Timeline owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    return db_path


def test_gateway_returns_multilabel_timeline_with_three_clock_semantics(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "星桥晚餐后来又提过哪些细节？", "limit": 10}
    )

    timeline = response["event_timeline"]
    assert timeline["schema_version"] == EVENT_TIMELINE_SCHEMA_VERSION
    assert timeline["status"] == "bounded_timeline"
    assert timeline["archive_first_mentioned_at"] == "2026-05-31T16:30:00Z"
    assert timeline["archive_first_mentioned_at_local"].startswith(
        "2026-06-01T00:30:00+08:00"
    )
    assert timeline["archive_first_scope"]["exhaustive"] is False
    assert timeline["mention_count"]["semantics"] == "at_least"

    by_id = {item["record_id"]: item for item in timeline["mentions"]}
    retelling = by_id["picnic-retelling"]
    assert {
        "retelling_or_recollection",
        "quoted_or_original_wording",
    } <= set(retelling["mention_types"])

    explicit_clock = by_id["picnic-clock"]
    assert explicit_clock["occurred_at"] == "2026-06-01T00:45:00Z"
    assert explicit_clock["occurred_at_local"].startswith(
        "2026-06-01T08:45:00+08:00"
    )
    assert explicit_clock["occurred_at_precision"] == "instant"
    assert "explicit_detail_addition" in explicit_clock["mention_types"]

    date_only = by_id["picnic-first"]
    assert date_only["occurred_at"] is None
    assert date_only["occurred_on"] == "2026-06-01"
    assert date_only["occurred_at_status"] == (
        "calendar_date_without_reliable_timezone"
    )


def test_same_day_similarity_stays_as_separate_unlinked_mentions(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "星桥晚餐", "limit": 10}
    )

    timeline = response["event_timeline"]
    assert timeline["linkage_policy"]["automatic_event_merge"] is False
    assert timeline["linkage_policy"]["same_day_similarity_is_not_identity"] is True
    assert len({item["node_id"] for item in timeline["mentions"]}) == len(
        timeline["mentions"]
    )
    assert all(item["same_event_as"] is None for item in timeline["mentions"])
    assert all(
        item["same_event_status"] == "not_automatically_asserted"
        for item in timeline["mentions"]
    )


def test_relative_weekday_uses_explicit_chengdu_reference_without_claiming_evidence():
    timeline = build_event_timeline(
        "上周三我们一起做了什么？",
        [],
        reference_instant="2026-08-24T11:00:00+08:00",
        reference_source="caller_as_of",
    )

    query_clock = timeline["query_clock"]
    assert query_clock["status"] == "resolved_calendar_scope"
    assert query_clock["matched_expression"] == "上周三"
    assert query_clock["resolved_on"] == "2026-08-19"
    assert query_clock["reference_at_local"].startswith(
        "2026-08-24T11:00:00+08:00"
    )
    assert query_clock["used_for_record_filtering"] is False


def test_last_month_resolves_to_calendar_range():
    timeline = build_event_timeline(
        "上个月我们做了什么？",
        [],
        reference_instant="2026-08-24T11:00:00+08:00",
        reference_source="caller_as_of",
    )

    query_clock = timeline["query_clock"]
    assert query_clock["resolved_start_on"] == "2026-07-01"
    assert query_clock["resolved_end_on"] == "2026-07-31"
    assert query_clock["precision"] == "month"


def test_relative_time_without_reference_remains_unresolved():
    timeline = build_event_timeline("昨晚我们做了什么？", [])

    query_clock = timeline["query_clock"]
    assert query_clock["status"] == (
        "unresolved_missing_timezone_aware_reference"
    )
    assert query_clock["resolved_on"] is None


def test_qualified_shared_event_adds_one_bounded_retelling_trace(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "我跟你第一次一起吃星桥晚餐是什么时候？",
            "limit": 10,
        }
    )

    passes = {item["pass"] for item in response["adaptive_recall"]["passes"]}
    assert "event_retelling_trace" in passes
    timeline_ids = {
        item["record_id"] for item in response["event_timeline"]["mentions"]
    }
    assert {"picnic-first", "picnic-retelling"} <= timeline_ids
    assert "unrelated-retelling-noise" not in timeline_ids
    assert response["adaptive_recall"]["query_passes_used"] == 2


def test_conflict_and_denial_node_survives_ordinary_timeline_limit():
    memories = [
        _record(
            f"ordinary-{index:02d}",
            f"星桥普通提及 {index}",
            f"2026-07-{index + 1:02d}T00:00:00Z",
        )
        for index in range(MAX_ORDINARY_TIMELINE_NODES + 3)
    ]
    memories.append(
        _record(
            "protected-correction",
            "更正：不是前面的说法。",
            "2026-08-01T00:00:00Z",
            conflict_group_id="conflict-synthetic",
        )
    )

    timeline = build_event_timeline("星桥", memories)
    ids = {item["record_id"] for item in timeline["mentions"]}
    assert timeline["mention_count"]["truncated"] is True
    assert timeline["mention_count"]["value"] == len(memories)
    assert "protected-correction" in ids
    protected = next(
        item
        for item in timeline["mentions"]
        if item["record_id"] == "protected-correction"
    )
    assert protected["preserve_even_when_ordinary_limit_reached"] is True


def test_detail_addition_requires_explicit_source_wording():
    assert "explicit_detail_addition" not in classify_mention_types(
        "星桥晚餐多了一条从未出现过的词语。"
    )
    assert "explicit_detail_addition" in classify_mention_types(
        "我再补充一个细节：星桥旁边有一盏灯。"
    )


def test_unbound_dates_and_relative_wording_do_not_invent_event_instants():
    timeline = build_event_timeline(
        "星桥",
        [
            _record(
                "unbound-date",
                "2026年6月1日只是档案编号，不代表事情发生时间。",
                "2026-07-01T00:00:00Z",
            ),
            _record(
                "relative-only",
                "昨晚又想起星桥，但这里没有可靠的时区锚点。",
                "2026-07-02T00:00:00Z",
            ),
        ],
    )

    by_id = {item["record_id"]: item for item in timeline["mentions"]}
    assert by_id["unbound-date"]["occurred_at"] is None
    assert by_id["unbound-date"]["occurred_on"] is None
    assert by_id["relative-only"]["occurred_at"] is None
    assert by_id["relative-only"]["occurred_on"] is None


def test_repeated_gateway_calls_have_identical_semantic_timeline(tmp_path):
    db_path = _database(tmp_path)
    gateway = ReadonlyGateway(str(db_path), OWNER)

    first = gateway.recall({"query": "星桥晚餐", "limit": 10})
    second = gateway.recall({"query": "星桥晚餐", "limit": 10})

    assert first["event_timeline"] == second["event_timeline"]
    assert (
        first["event_timeline"]["semantic_payload_sha256"]
        == second["event_timeline"]["semantic_payload_sha256"]
    )
