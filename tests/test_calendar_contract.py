"""End-to-end calendar invariants, with synthetic evidence only."""

from datetime import datetime
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory.adaptive_recall import adaptive_recall
from backend.memory.event_timeline import resolve_query_calendar_scope
from backend.memory.identity import register_agent
from backend.memory.recall_debug import render_recall_debug, run_recall_debug
from backend.memory.records_v1 import import_record_package
from backend.telegram_bot.readonly_api import ReadonlyApiConfig, create_readonly_app


OWNER = "agt-calendar-contract"
REFERENCE = "2026-11-10T10:00:00+08:00"


@pytest.fixture
def calendar_db(tmp_path):
    records = []
    for index, (day, content) in enumerate([
        ("2026-10-26", "今天我们一起修好了窗边的台灯。"),
        ("2026-09-25", "今天我们一起吃月饼，散步看月亮。"),
        ("2026-11-04", "今天我们整理了书架。"),
        ("2026-06-01", "今天我们在桥边吃晚餐。"),
        ("2026-10-27", "后来回忆昨天，我们在做什么？我们一起修台灯。"),
    ]):
        records.append({
            "record_id": f"day-{day}",
            "source_kind": "codex-app-server-v1" if index == 4 else "room-ferry-backup-v1",
            "source_ref": f"synthetic://calendar/{index}",
            "conversation_id": f"room-{index}",
            "branch_id": "main",
            "message_id": f"message-{index}",
            "role": "user",
            "content": content,
            "created_at": f"{day}T01:00:00Z",
            "verified": False,
            "authority": "synthetic-unverified",
            "source_cutoff_at": "2026-11-10T00:00:00Z",
            "conflict_group_id": None,
        })
    db = tmp_path / "calendar.sqlite3"
    package = tmp_path / "calendar.json"
    package.write_text(json.dumps({"schema_version": "echo-pact-records-v1", "records": records}), encoding="utf-8")
    register_agent(OWNER, "Calendar contract", actor="test", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id=OWNER, actor="test")
    return db


@pytest.fixture
def fixed_clock(monkeypatch):
    from backend.memory import adaptive_recall as recall_module
    from backend.mcp import readonly_server

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(REFERENCE).astimezone(tz)

    monkeypatch.setattr(recall_module, "datetime", FixedDateTime)
    monkeypatch.setattr(readonly_server, "datetime", FixedDateTime)


@pytest.mark.parametrize("query,day", [
    ("10月26号我们在做什么？", "2026-10-26"),
    ("中秋节那天我们在干嘛？", "2026-09-25"),
    ("上周三我们做了什么？", "2026-11-04"),
    ("六月一号我们做了什么？", "2026-06-01"),
])
def test_bot_mcp_and_debug_share_calendar_without_caller_clock(calendar_db, fixed_clock, query, day):
    before = hashlib.sha256(calendar_db.read_bytes()).hexdigest()
    gateway = ReadonlyGateway(str(calendar_db), OWNER).recall({"query": query, "limit": 10})
    debug = run_recall_debug(query, db_path=str(calendar_db), agent_id=OWNER, limit=10)
    client = TestClient(create_readonly_app(ReadonlyApiConfig(str(calendar_db), OWNER, result_limit=10)))
    http = client.post("/recall-debug", json={"query": query})
    assert http.status_code == 200
    for result in (gateway, debug, http.json()):
        assert result["temporal_scope"]["resolved_on"] == day
        assert result["record_time_filter"]["applied"] is True
        assert [m["record_id"] for m in result["memories"]] == [f"day-{day}"]
    assert hashlib.sha256(calendar_db.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("query,status", [
    ("2026年2月30号我们做了什么？", "unresolved_invalid_calendar_date"),
    ("2025年中秋节我们做了什么？", "unresolved_unsupported_named_calendar_year"),
    ("6月1号和6月3号我们做了什么？", "unresolved_multiple_calendar_scopes"),
])
def test_unresolved_date_never_falls_back_to_unbounded_recall(calendar_db, query, status):
    result = adaptive_recall(query, agent_id=OWNER, db_path=str(calendar_db),
                             as_of=REFERENCE, read_only=True, debug=True)
    assert result["temporal_scope"]["status"] == status
    assert result["memories"] == []
    assert result["query_clarification"]["reason"] == "calendar_scope_unresolved"
    assert result["adaptive_recall"]["query_passes_used"] == 0
    panel = render_recall_debug(result)
    assert "没有开始检索" in panel
    assert "缺少明确主题" not in panel


@pytest.mark.parametrize("query,day", [
    ("去年6月1号我们做了什么？", "2025-06-01"),
    ("2025年六月一号我们做了什么？", "2025-06-01"),
    ("2026年中秋节我们做了什么？", "2026-09-25"),
])
def test_year_in_question_wins_over_reference_year(query, day):
    scope = resolve_query_calendar_scope(query, REFERENCE, "test", used_for_record_filtering=True)
    assert scope["resolved_on"] == day


def test_month_day_without_reference_is_unresolved_not_not_requested():
    scope = resolve_query_calendar_scope("6月1号我们做了什么？", None, None)
    assert scope["status"] == "unresolved_missing_timezone_aware_reference"


def test_explicit_named_year_does_not_need_server_year():
    scope = resolve_query_calendar_scope("2026年中秋节我们做了什么？", None, None)
    assert scope["resolved_on"] == "2026-09-25"


def test_coverage_cutoff_uses_chengdu_day_not_utc_day():
    from backend.memory.adaptive_recall import _apply_temporal_coverage_guard

    response = {"coverage": {"latest_imported_record_at": "2026-11-06T20:00:00Z"},
                "memories": [{"record_id": "already-november-seven-in-chengdu"}]}
    result = _apply_temporal_coverage_guard("2026年11月7日我们做了什么？", response)
    assert result["memories"]
    assert result["temporal_coverage"]["coverage_gap"] is False
