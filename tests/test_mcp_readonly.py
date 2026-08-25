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
    as_of_description = recall["inputSchema"]["properties"]["as_of"][
        "description"
    ].lower()
    assert "timezone-aware" in as_of_description
    assert "last wednesday" in as_of_description
    assert "not a replacement for the current conversation" in instructions
    assert "answering from a vague impression" in instructions
    assert "do not call merely because a past topic is mentioned" in instructions
    assert "pass only the semantic memory question" in instructions


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
    bounded = _bounded_result(
        {
            "memories": [
                {
                    "content": "representative",
                    "event_evidence": [{"content": original}],
                }
            ]
        }
    )

    evidence = bounded["memories"][0]["event_evidence"][0]
    assert evidence["content_chars"] == len(original)
    assert evidence["content_truncated"] is True
    assert evidence["content"].endswith("[Echo Pact: content truncated]")


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
