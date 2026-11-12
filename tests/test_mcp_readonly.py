"""M6-01 local read-only MCP gateway tests (synthetic data only)."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from backend.mcp.readonly_server import (
    MAX_CONTENT_CHARS,
    ReadonlyGateway,
    SERVER_INSTRUCTIONS,
    StdioMcpServer,
    _bounded_result,
    tool_definitions,
)
from backend.memory.identity import register_agent
from backend.memory.records_v1 import _connect_readonly, import_record_package


OWNER = "agt-mcp-owner"
OUTSIDER = "agt-mcp-outsider"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(record_id: str, content: str, created_at: str):
    return {
        "record_id": record_id,
        "source_kind": "synthetic_conversation",
        "source_ref": f"synthetic://mcp/{record_id}",
        "conversation_id": "synthetic-mcp-conversation",
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
def mcp_db(tmp_path):
    # Special characters lock the Windows file-URI escaping regression.
    db_path = tmp_path / "echo pact # 100%.sqlite3"
    package_path = tmp_path / "records.json"
    package_path.write_text(
        json.dumps(
            {
                "schema_version": "echo-pact-records-v1",
                "records": [
                    _record(
                        "mcp-001",
                        "灯塔暗号是 FERRY-MCP-2042，属于完全虚构测试。",
                        "2026-08-01T00:00:00Z",
                    ),
                    _record(
                        "mcp-002",
                        "长内容回归 " + ("甲" * (MAX_CONTENT_CHARS + 80)),
                        "2026-08-02T00:00:00Z",
                    ),
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Synthetic MCP owner", actor="test", db_path=str(db_path))
    register_agent(
        OUTSIDER, "Synthetic MCP outsider", actor="test", db_path=str(db_path)
    )
    summary = import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    assert summary["added"] == 2
    return db_path


def test_tool_contract_is_read_only_and_has_no_identity_argument():
    tools = tool_definitions()
    assert {tool["name"] for tool in tools} == {
        "recall_context",
        "expand_record",
        "memory_coverage",
    }
    for tool in tools:
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        assert "agent_id" not in tool["inputSchema"].get("properties", {})


def test_recall_contract_routes_context_gaps_without_overcalling():
    recall = next(
        tool for tool in tool_definitions() if tool["name"] == "recall_context"
    )
    description = recall["description"].lower()
    instructions = SERVER_INSTRUCTIONS.lower()

    assert (
        "current conversation context or reliable memory is insufficient"
        in description
    )
    assert "before saying you do not remember or asking for a hint" in description
    assert "image" in description and "explicitly asks to create or edit" in description
    query_description = recall["inputSchema"]["properties"]["query"][
        "description"
    ].lower()
    assert "only the semantic" in query_description
    assert "number of calls" in query_description
    assert "speaker's i/you direction" in query_description
    as_of_description = recall["inputSchema"]["properties"]["as_of"][
        "description"
    ].lower()
    assert "timezone-aware" in as_of_description
    assert "last wednesday" in as_of_description
    assert "not a replacement for the current conversation" in instructions
    assert "answering from a vague impression" in instructions
    assert "do not call merely because a past topic is mentioned" in instructions
    assert "pass only the semantic memory question" in instructions
    assert "named_collection" in description
    assert "source_provenance" in description
    assert "confirmed items separate from candidates" in instructions
    assert "query_clarification" in instructions
    assert "not an automatic ruling about tone, intent, or truth" in instructions
    assert "never claim to see or describe image content" in instructions
    assert "one support unit, not independent corroboration" in instructions
    assert "does not by itself prove real-world event identity or truth" in instructions
    assert "result limit counts distinct support units" in instructions
    assert "coverage continues to count immutable raw records" in instructions


def test_bounded_result_preserves_source_provenance_without_record_content():
    provenance = {
        "schema_version": "echo-pact-source-provenance-v1",
        "sources": [
            {
                "source_kind": "room-ferry-backup-v1",
                "source_channel": "chatgpt",
                "record_ids": ["record-a"],
            }
        ],
        "exact_cross_source_mirror_groups": [],
    }

    bounded = _bounded_result(
        {
            "memories": [{"record_id": "record-a", "content": "private text"}],
            "source_provenance": provenance,
        }
    )

    assert bounded["source_provenance"] == provenance
    assert "private text" not in json.dumps(
        bounded["source_provenance"], ensure_ascii=False
    )


def test_bounded_result_truncates_supporting_phase_content():
    long_content = "过程" * (MAX_CONTENT_CHARS + 20)
    bounded = _bounded_result(
        {
            "memories": [
                {
                    "record_id": "final",
                    "content": "正文",
                    "supporting_phase_records": [
                        {"record_id": "thoughts", "content": long_content}
                    ],
                }
            ]
        }
    )

    supporting = bounded["memories"][0]["supporting_phase_records"][0]
    assert supporting["content_chars"] == len(long_content)
    assert supporting["content_truncated"] is True
    assert supporting["content"].startswith(long_content[:MAX_CONTENT_CHARS])
    assert supporting["content"].endswith("[Echo Pact: content truncated]")


def test_gateway_anchors_relative_time_to_local_server_clock(mcp_db, monkeypatch):
    from backend.mcp import readonly_server
    real_datetime = readonly_server.datetime

    class FixedDateTime:
        @classmethod
        def now(cls, tz):
            return real_datetime.fromisoformat(
                "2026-08-24T11:00:00+08:00"
            ).astimezone(tz)

    monkeypatch.setattr(readonly_server, "datetime", FixedDateTime)
    recalled = ReadonlyGateway(str(mcp_db), OWNER).recall(
        {"query": "上周三我们一起做了什么？", "limit": 1}
    )

    query_clock = recalled["event_timeline"]["query_clock"]
    assert query_clock["reference_source"] == "mcp_server_clock"
    assert query_clock["resolved_on"] == "2026-08-19"


def test_gateway_anchors_month_day_without_year_to_server_clock(mcp_db, monkeypatch):
    from backend.mcp import readonly_server
    real_datetime = readonly_server.datetime

    class FixedDateTime:
        @classmethod
        def now(cls, tz):
            return real_datetime.fromisoformat(
                "2026-10-05T18:00:00+08:00"
            ).astimezone(tz)

    monkeypatch.setattr(readonly_server, "datetime", FixedDateTime)
    recalled = ReadonlyGateway(str(mcp_db), OWNER).recall(
        {"query": "六月一号我们做了什么？", "limit": 1}
    )

    query_clock = recalled["event_timeline"]["query_clock"]
    assert query_clock["reference_source"] == "mcp_server_clock"
    assert query_clock["resolved_on"] == "2026-06-01"


def test_gateway_recalls_owner_only_without_changing_database(mcp_db):
    before = _sha256(mcp_db)
    owner = ReadonlyGateway(str(mcp_db), OWNER)
    outsider = ReadonlyGateway(str(mcp_db), OUTSIDER)

    coverage = owner.coverage()
    assert coverage["visible_record_count"] == 2
    recalled = owner.recall(
        {"query": "FERRY-MCP-2042", "limit": 5, "include_projection": True}
    )
    assert [item["record_id"] for item in recalled["memories"]] == ["mcp-001"]
    assert recalled["memories"][0]["projection_status"] == "unprojected"
    assert recalled["memories"][0]["source_ref"] == "synthetic://mcp/mcp-001"

    assert outsider.coverage()["visible_record_count"] == 0
    assert outsider.recall({"query": "FERRY-MCP-2042"})["memories"] == []
    assert _sha256(mcp_db) == before


def test_gateway_bounds_long_memory_content(mcp_db):
    recalled = ReadonlyGateway(str(mcp_db), OWNER).recall(
        {"query": "长内容回归", "include_projection": False}
    )
    memory = recalled["memories"][0]
    assert memory["content_truncated"] is True
    assert memory["content_chars"] > MAX_CONTENT_CHARS
    assert memory["content"].endswith("[Echo Pact: content truncated]")


def test_gateway_bounds_shared_event_evidence_content():
    original = "乙" * (MAX_CONTENT_CHARS + 40)
    relationship_direction = {
        "relation": "guided_visit",
        "query_speaker_role": "guest",
    }
    bounded = _bounded_result(
        {
            "event_recall": {
                "relationship_direction": relationship_direction,
            },
            "memories": [
                {
                    "content": "representative",
                    "event_evidence": [{"content": original}],
                    "relationship_direction": relationship_direction,
                }
            ]
        }
    )

    evidence = bounded["memories"][0]["event_evidence"][0]
    assert evidence["content_chars"] == len(original)
    assert evidence["content_truncated"] is True
    assert evidence["content"].endswith("[Echo Pact: content truncated]")
    assert bounded["event_recall"]["relationship_direction"] == relationship_direction
    assert bounded["memories"][0]["relationship_direction"] == relationship_direction


def test_gateway_bounds_outside_scope_retelling_content():
    original = "丙" * (MAX_CONTENT_CHARS + 40)
    bounded = _bounded_result(
        {
            "temporal_scope": {
                "outside_scope_retellings": [
                    {
                        "content": original,
                        "conversation_context": [{"content": original}],
                    }
                ]
            }
        }
    )

    retelling = bounded["temporal_scope"]["outside_scope_retellings"][0]
    assert retelling["content_truncated"] is True
    assert retelling["content"].endswith("[Echo Pact: content truncated]")
    assert retelling["conversation_context"][0]["content_truncated"] is True


def test_readonly_open_refuses_old_schema_without_migrating(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 6):
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (version, f"synthetic-v{version}", "2026-08-01T00:00:00Z"),
            )
        conn.execute("CREATE TABLE records_v1 (id INTEGER PRIMARY KEY)")
    before = _sha256(db_path)
    with pytest.raises(RuntimeError, match="schema"):
        _connect_readonly(str(db_path))
    assert _sha256(db_path) == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 5


def _exchange(process, message):
    process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def test_stdio_mcp_handshake_and_calls_are_compatible(mcp_db):
    env = os.environ.copy()
    env.update(
        {
            "ECHO_PACT_MCP_DB_PATH": str(mcp_db),
            "ECHO_PACT_MCP_AGENT_ID": OWNER,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    repo = Path(__file__).resolve().parents[1]
    launcher = repo / "scripts" / "echo_pact_mcp.py"
    process = subprocess.Popen(
        [sys.executable, str(launcher)],
        # Codex may start the configured server outside the repository.  The
        # launcher must therefore establish its own import root.
        cwd=mcp_db.parent,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        initialized = _exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        assert initialized["result"]["protocolVersion"] == "2025-11-25"
        assert initialized["result"]["capabilities"] == {
            "tools": {"listChanged": False}
        }
        assert initialized["result"]["instructions"] == SERVER_INSTRUCTIONS

        # Initialized is a notification and therefore deliberately has no reply.
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
        )
        process.stdin.flush()

        listed = _exchange(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert {tool["name"] for tool in listed["result"]["tools"]} == {
            "recall_context",
            "expand_record",
            "memory_coverage",
        }

        called = _exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "recall_context",
                    "arguments": {"query": "FERRY-MCP-2042", "limit": 1},
                },
            },
        )
        assert called["result"]["isError"] is False
        assert called["result"]["structuredContent"]["memories"][0][
            "record_id"
        ] == "mcp-001"
        brief = _exchange(process, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "recall_context", "arguments": {
                "query": "FERRY-MCP-2042", "detail": "brief",
            }},
        })["result"]["structuredContent"]
        item = brief["memories"][0]
        expanded = _exchange(process, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "expand_record", "arguments": {
                "record_id": item["record_id"],
                "expected_content_sha256": item["content_sha256"],
            }},
        })["result"]
        assert expanded["isError"] is False
        assert expanded["structuredContent"]["content"] == called["result"]["structuredContent"]["memories"][0]["content"]
    finally:
        process.stdin.close()
        process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
    assert process.returncode == 0


def test_tool_argument_validation_does_not_echo_query(mcp_db):
    server = StdioMcpServer(ReadonlyGateway(str(mcp_db), OWNER))
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "recall_context",
                "arguments": {"query": "PRIVATE-SYNTHETIC", "agent_id": OUTSIDER},
            },
        }
    )
    assert response["result"]["isError"] is True
    rendered = json.dumps(response, ensure_ascii=False)
    assert "PRIVATE-SYNTHETIC" not in rendered
    assert OUTSIDER not in rendered


def test_brief_budget_covers_metadata_and_mcp_text_duplication():
    import copy
    from backend.mcp.recall_packets import (
        MAX_BRIEF_JSON_BYTES, MAX_BRIEF_MCP_RESULT_BYTES, brief_result, json_bytes, tool_result,
    )
    raw = {
        "memories": [dict(_record(f"large-{n}", "长😀\n\"\\" * 2000, "2026-08-01T00:00:00Z"),
                          conversation_context=[{"content": "CONTEXT-SECRET" * 5000}]) for n in range(10)],
        "event_timeline": {"mentions": [{"content_excerpt": "TIMELINE-SECRET" * 5000,
                                           "preserve_even_when_ordinary_limit_reached": True}] * 100},
        "source_provenance": {"evidence_records": [{"source_ref": "PROVENANCE-SECRET" * 5000}] * 100},
        "recall_debug": {"body": "DEBUG-SECRET" * 5000},
        "temporal_scope": {"status": "resolved_calendar_scope", "filter_start_at": "2026-07-31T16:00:00Z",
                           "filter_end_at_exclusive": "2026-08-01T16:00:00Z",
                           "outside_scope_retellings": [{"content": "OUTSIDE-SECRET" * 5000}]},
        "coverage": {"coverage_gap": True, "coverage_status": "verified_cutoff_unknown"},
    }
    before = copy.deepcopy(raw)
    brief = brief_result(raw)
    assert raw == before
    assert json_bytes(brief) <= MAX_BRIEF_JSON_BYTES
    assert json_bytes(tool_result(brief)) <= MAX_BRIEF_MCP_RESULT_BYTES
    rendered = json.dumps(brief, ensure_ascii=False)
    assert "SECRET" not in rendered
    assert len(brief["memories"]) == 10
    assert brief["coverage"] == raw["coverage"]
    assert brief["temporal_scope"]["filter_start_at"] == "2026-07-31T16:00:00Z"
    assert brief["temporal_scope"]["outside_scope_retellings_omitted"] == 1
    assert brief["event_timeline"]["conflict_or_correction_mentions_omitted"] == 100
    assert brief["memories"][0]["content_chars"] == 10_000
    assert brief["memories"][0]["content_truncated"] is True
    assert brief["memories"][0]["content_sha256"] == hashlib.sha256(raw["memories"][0]["content"].encode()).hexdigest()


def test_brief_oversize_metadata_is_explicit_and_does_not_corrupt_ids():
    from backend.mcp.recall_packets import brief_result, json_bytes, MAX_BRIEF_JSON_BYTES
    raw = {"memories": [dict(_record(f"record-{n}", "preview", "2026-08-01T00:00:00Z"),
                              authority="x" * 40_000, source_ref="x" * 40_000) for n in range(10)]}
    raw["memories"].append(_record("x" * 300, "not-expandable", "2026-08-01T00:00:00Z"))
    brief = brief_result(raw)
    assert json_bytes(brief) <= MAX_BRIEF_JSON_BYTES
    assert brief["metadata_incomplete"] is True
    assert brief["omitted"]["memory_count"] >= 1
    for item in brief["memories"]:
        assert "authority" not in item and "authority" in item["fields_omitted"]
        assert item["record_id"].startswith("record-")


def test_brief_full_parity_and_exact_unicode_pagination(mcp_db, monkeypatch):
    from backend.mcp import readonly_server
    gateway = ReadonlyGateway(str(mcp_db), OWNER)
    before = _sha256(mcp_db)
    args = {"query": "长内容回归", "include_projection": False}
    full = gateway.recall(args)
    assert full == gateway.recall({**args, "detail": "full"})
    brief = gateway.recall({**args, "detail": "brief"})
    item = brief["memories"][0]
    assert item["record_id"] == full["memories"][0]["record_id"]
    assert item["content_chars"] == full["memories"][0]["content_chars"]
    assert item["content_chars"] > len(full["memories"][0]["content"])
    assert item["verified"] is False
    assert item["source_channel"] == "other"

    def no_search(*args, **kwargs):
        pytest.fail("exact expansion must not perform another recall")
    monkeypatch.setattr(readonly_server, "adaptive_recall", no_search)
    parts, offset = [], 0
    while offset is not None:
        page = gateway.expand({"record_id": item["record_id"], "expected_content_sha256": item["content_sha256"],
                               "offset": offset, "length": 311})
        assert page["record"]["record_id"] == item["record_id"]
        assert page["offset"] == offset
        assert page["end_offset"] == offset + len(page["content"])
        parts.append(page["content"])
        offset = page["next_offset"]
    original = "".join(parts)
    assert original == "长内容回归 " + "甲" * (MAX_CONTENT_CHARS + 80)
    assert hashlib.sha256(original.encode()).hexdigest() == item["content_sha256"]
    assert _sha256(mcp_db) == before


def test_exact_expansion_hidden_absent_disabled_and_revoked_are_unavailable(mcp_db):
    from backend.memory.identity import grant_access, revoke_access, set_agent_enabled
    outsider = ReadonlyGateway(str(mcp_db), OUTSIDER)
    for record_id in ("mcp-001", "does-not-exist", "' OR 1=1 --"):
        with pytest.raises(ValueError, match="^record unavailable$"):
            outsider.expand({"record_id": record_id})
    grant_access("mcp-002", OUTSIDER, actor="test", db_path=str(mcp_db))
    first = outsider.expand({"record_id": "mcp-002", "length": 10})
    with sqlite3.connect(mcp_db) as conn:
        grant_seq = conn.execute("SELECT MAX(event_seq) FROM record_visibility_events WHERE event_kind = 'grant'").fetchone()[0]
    revoke_access("mcp-002", OUTSIDER, grant_seq, actor="test", db_path=str(mcp_db))
    with pytest.raises(ValueError, match="^record unavailable$"):
        outsider.expand({"record_id": "mcp-002", "offset": first["next_offset"],
                         "expected_content_sha256": first["content_sha256"]})
    set_agent_enabled(OWNER, False, actor="test", db_path=str(mcp_db))
    with pytest.raises(ValueError, match="^record unavailable$"):
        ReadonlyGateway(str(mcp_db), OWNER).expand({"record_id": "mcp-001"})


@pytest.mark.parametrize("args", [
    {"offset": -1}, {"offset": True}, {"offset": 0.5}, {"offset": 2**100},
    {"length": 0}, {"length": MAX_CONTENT_CHARS + 1}, {"length": False},
    {"offset": 1}, {"expected_content_sha256": "invalid"}, {"agent_id": OUTSIDER},
])
def test_expand_rejects_invalid_inputs_before_database_open(monkeypatch, args):
    from backend.mcp import readonly_server
    monkeypatch.setattr(readonly_server, "_connect_readonly", lambda *args: pytest.fail("must validate first"))
    with pytest.raises(ValueError):
        ReadonlyGateway("unused", OWNER).expand({"record_id": "mcp-001", **args})


def test_expand_fingerprint_mismatch_and_bounds(mcp_db):
    gateway = ReadonlyGateway(str(mcp_db), OWNER)
    with pytest.raises(ValueError, match="fingerprint changed"):
        gateway.expand({"record_id": "mcp-001", "expected_content_sha256": "0" * 64})
    first = gateway.expand({"record_id": "mcp-001"})
    at_end = gateway.expand({"record_id": "mcp-001", "offset": first["content_chars"],
                             "expected_content_sha256": first["content_sha256"]})
    assert at_end["content"] == "" and at_end["next_offset"] is None
    with pytest.raises(ValueError, match="exceeds record length"):
        gateway.expand({"record_id": "mcp-001", "offset": first["content_chars"] + 1,
                        "expected_content_sha256": first["content_sha256"]})


def test_record_page_is_exact_and_byte_bounded_with_escaped_text():
    from backend.mcp.recall_packets import MAX_RECORD_JSON_BYTES, json_bytes, record_page
    content = "\x00😀\n\"\\" * 3000
    record = _record("unicode-page", content, "2026-08-01T00:00:00Z")
    offset, parts = 0, []
    while offset is not None:
        page = record_page(record, offset=offset, length=4000)
        assert json_bytes(page) <= MAX_RECORD_JSON_BYTES
        assert page["content"] == content[offset:page["end_offset"]]
        assert page["end_offset"] > offset
        parts.append(page["content"])
        offset = page["next_offset"]
    assert "".join(parts) == content


def test_brief_keeps_unresolved_and_outside_coverage_honest(mcp_db):
    gateway = ReadonlyGateway(str(mcp_db), OWNER)
    unresolved = gateway.recall({"query": "2026年2月30日做了什么？", "detail": "brief"})
    assert unresolved["memories"] == []
    assert unresolved["query_clarification"]["reason"] == "calendar_scope_unresolved"
    assert unresolved["record_time_filter"]["applied"] is False
    outside = gateway.recall({"query": "2027年1月1日做了什么？", "detail": "brief"})
    assert outside["memories"] == []
    assert outside["coverage"]["coverage_gap"] is True
    assert outside["temporal_coverage"]["status"] == "outside_imported_coverage"


@pytest.mark.parametrize("detail", [None, "summary", True, [], {}])
def test_invalid_detail_rejected_before_search(monkeypatch, detail):
    from backend.mcp import readonly_server
    monkeypatch.setattr(readonly_server, "adaptive_recall", lambda *args, **kw: pytest.fail("must validate first"))
    with pytest.raises(ValueError, match="detail"):
        ReadonlyGateway("unused", OWNER).recall({"query": "synthetic", "detail": detail})
