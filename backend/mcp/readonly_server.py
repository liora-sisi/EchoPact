"""Dependency-free, local-only MCP gateway for Echo Pact recall.

The server speaks MCP over newline-delimited JSON-RPC on stdin/stdout.  It
publishes only read-only tools, fixes identity at process startup, opens SQLite
with ``mode=ro`` and refuses schema drift instead of running migrations.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from backend.memory.adaptive_recall import adaptive_recall
from backend.memory.event_timeline import (
    CHENGDU_TIMEZONE_NAME,
    query_uses_relative_time,
)
from backend.memory.identity import visible_coverage
from backend.memory.records_v1 import _connect_readonly


_CHENGDU_TIMEZONE = timezone(timedelta(hours=8), CHENGDU_TIMEZONE_NAME)


LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    LATEST_PROTOCOL_VERSION,
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
}
SERVER_VERSION = "0.1.1"
MAX_QUERY_CHARS = 2_000
MAX_RESULT_LIMIT = 10
MAX_CONTENT_CHARS = 4_000

SERVER_INSTRUCTIONS = (
    "Echo Pact is a read-only external memory fallback, not a replacement for "
    "the current conversation. Prefer reliable in-context information. Call "
    "recall_context when the current context or reliable memory is insufficient "
    "for a user-specific past event, prior conversation, exact wording, date, "
    "decision, shared history, or the origin of a past artifact. Before saying "
    "you do not remember, answering from a vague impression, or asking the user "
    "for a hint, call recall_context once when the archive could help. A mention "
    "of a past image, drawing, photo, song, gift, or other artifact is still a "
    "memory question unless the user explicitly asks to create or edit content. "
    "Pass only the semantic memory question in query. Do not copy tool-use "
    "instructions, call-count rules, answer formatting, or evidence-reporting "
    "boilerplate into the search text. Preserve the question speaker's first/"
    "second-person relationship direction; do not rewrite I/you roles into the "
    "assistant narrator's perspective. For a relative time phrase, provide a "
    "reliable timezone-aware as_of when available; otherwise the local gateway "
    "anchors it to its Asia/Shanghai server clock. "
    "Do not call merely because a past topic is mentioned when the current "
    "conversation already supports the answer. Use memory_coverage to check what "
    "the archive can honestly support. Treat verified, authority, "
    "source_cutoff_at, coverage_gap and conflict metadata as evidence boundaries. "
    "Never present unverified archive text as a user-confirmed fact. The server "
    "cannot write, delete, migrate or change identity; it returns only records "
    "visible to its fixed startup agent."
)


TOOLS = (
    {
        "name": "recall_context",
        "title": "Recall Echo Pact context",
        "description": (
            "Search the fixed agent's visible Echo Pact evidence when current "
            "conversation context or reliable memory is insufficient for a "
            "user-specific past event, prior conversation, exact wording, date, "
            "decision, shared history, or a past artifact's origin. Before saying "
            "you do not remember or asking for a hint, call this read-only tool "
            "once if the archive could help, even when words such as image, "
            "drawing, photo, song, or gift appear, unless the user explicitly "
            "asks to create or edit. Results include source, branch, verification, "
            "cutoff, coverage, and optional Claim annotations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_QUERY_CHARS,
                    "description": (
                        "Only the semantic natural-language memory question. "
                        "Exclude instructions about tool use, number of calls, "
                        "answer format, or evidence reporting. Preserve the "
                        "original question speaker's I/you direction."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULT_LIMIT,
                    "default": 5,
                },
                "as_of": {
                    "type": "string",
                    "description": (
                        "Optional timezone-aware ISO-8601 reference instant. "
                        "It anchors relative phrases such as yesterday, last "
                        "Wednesday, or last month and also bounds knowledge "
                        "coverage. Never provide a timezone-naive value."
                    ),
                },
                "include_projection": {
                    "type": "boolean",
                    "default": True,
                    "description": "Attach visible Claim/conflict annotations.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memory_coverage",
        "title": "Inspect Echo Pact memory coverage",
        "description": (
            "Return the current agent's visible record count and honest "
            "verified/latest-imported cutoff metadata without returning chat text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
)


class GatewayConfigError(RuntimeError):
    """The fixed local gateway configuration is missing or unsafe."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _truncate_text(value: Any) -> Any:
    if not isinstance(value, str) or len(value) <= MAX_CONTENT_CHARS:
        return value
    return value[:MAX_CONTENT_CHARS] + "\n[Echo Pact: content truncated]"


def _bounded_result(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Bound tool output while preserving provenance and coverage metadata."""

    bounded = copy.deepcopy(dict(result))

    def bound_memory(memory: Dict[str, Any]) -> None:
        original = memory.get("content")
        if isinstance(original, str):
            memory["content_chars"] = len(original)
            memory["content_truncated"] = len(original) > MAX_CONTENT_CHARS
            memory["content"] = _truncate_text(original)
        for claim in memory.get("claims", []):
            if "content" in claim:
                claim["content"] = _truncate_text(claim["content"])
        for field in ("conversation_context", "event_evidence"):
            for context_record in memory.get(field, []):
                original = context_record.get("content")
                if isinstance(original, str):
                    context_record["content_chars"] = len(original)
                    context_record["content_truncated"] = (
                        len(original) > MAX_CONTENT_CHARS
                    )
                    context_record["content"] = _truncate_text(original)

    for memory in bounded.get("memories", []):
        bound_memory(memory)
    temporal_scope = bounded.get("temporal_scope")
    if isinstance(temporal_scope, dict):
        for memory in temporal_scope.get("outside_scope_retellings", []):
            bound_memory(memory)
    return bounded


@dataclass(frozen=True)
class ReadonlyGateway:
    db_path: str
    agent_id: str

    @classmethod
    def from_environment(cls) -> "ReadonlyGateway":
        db_path = os.getenv("ECHO_PACT_MCP_DB_PATH", "").strip()
        agent_id = os.getenv("ECHO_PACT_MCP_AGENT_ID", "").strip()
        if not db_path:
            raise GatewayConfigError("ECHO_PACT_MCP_DB_PATH is not configured")
        if not agent_id:
            raise GatewayConfigError("ECHO_PACT_MCP_AGENT_ID is not configured")
        return cls(db_path=db_path, agent_id=agent_id)

    def coverage(self) -> Dict[str, Any]:
        conn = _connect_readonly(self.db_path)
        try:
            coverage = visible_coverage(conn, self.agent_id)
            visible_count = coverage.pop("_visible_record_count")
            return {
                "schema_version": "echo-pact-mcp-coverage-v1",
                "agent_id": self.agent_id,
                "visible_record_count": visible_count,
                **coverage,
            }
        finally:
            conn.close()

    def validate(self) -> None:
        self.coverage()

    def recall(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        allowed = {"query", "limit", "as_of", "include_projection"}
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise ValueError("unknown arguments: " + ", ".join(unknown))
        query = _required_text(arguments.get("query"), "query")
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query must not exceed {MAX_QUERY_CHARS} characters")
        limit = arguments.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= MAX_RESULT_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_RESULT_LIMIT}")
        as_of = arguments.get("as_of")
        reference_time_source = "caller_as_of" if as_of is not None else None
        if as_of is None and query_uses_relative_time(query):
            as_of = datetime.now(_CHENGDU_TIMEZONE).isoformat()
            reference_time_source = "mcp_server_clock"
        include_projection = arguments.get("include_projection", True)
        if not isinstance(include_projection, bool):
            raise ValueError("include_projection must be a boolean")

        result = adaptive_recall(
            query,
            agent_id=self.agent_id,
            limit=limit,
            as_of=as_of,
            db_path=self.db_path,
            read_only=True,
            include_projection=include_projection,
            reference_time_source=reference_time_source,
        )
        return _bounded_result(result)


def tool_definitions() -> list[Dict[str, Any]]:
    return [copy.deepcopy(tool) for tool in TOOLS]


def _tool_result(payload: Mapping[str, Any], *, is_error: bool = False) -> Dict[str, Any]:
    structured = dict(payload)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "structuredContent": structured,
        "isError": is_error,
    }


def _safe_error(message: str) -> Dict[str, Any]:
    return _tool_result({"error": message}, is_error=True)


class StdioMcpServer:
    def __init__(self, gateway: ReadonlyGateway):
        self.gateway = gateway

    def handle(self, message: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request")

        # Notifications never receive a JSON-RPC response.
        if request_id is None:
            return None

        try:
            if method == "initialize":
                params = message.get("params") or {}
                requested = params.get("protocolVersion")
                protocol = (
                    requested
                    if requested in SUPPORTED_PROTOCOL_VERSIONS
                    else LATEST_PROTOCOL_VERSION
                )
                self.gateway.validate()
                result = {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "echo-pact-readonly",
                        "title": "Echo Pact Read-only Memory",
                        "version": SERVER_VERSION,
                    },
                    "instructions": SERVER_INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_definitions()}
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, Mapping):
                    raise ValueError("tool arguments must be an object")
                if name == "recall_context":
                    result = _tool_result(self.gateway.recall(arguments))
                elif name == "memory_coverage":
                    if arguments:
                        raise ValueError("memory_coverage accepts no arguments")
                    result = _tool_result(self.gateway.coverage())
                else:
                    raise ValueError("unknown tool")
            else:
                return self._error(request_id, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ValueError as exc:
            if method == "tools/call":
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": _safe_error(str(exc)),
                }
            return self._error(request_id, -32602, "Invalid params")
        except Exception as exc:
            # stderr is local diagnostics only; arguments, record text and
            # credentials are deliberately never logged.
            print(
                f"Echo Pact MCP {method} failed: {type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            if method == "tools/call":
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": _safe_error("Echo Pact read-only operation failed"),
                }
            return self._error(request_id, -32603, "Internal error")

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def run(self) -> int:
        for raw_line in sys.stdin.buffer:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8"))
                if not isinstance(message, Mapping):
                    raise ValueError
                response = self.handle(message)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                response = self._error(None, -32700, "Parse error")
            if response is not None:
                sys.stdout.write(
                    json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                sys.stdout.flush()
        return 0


def main() -> int:
    # MCP stdio is UTF-8 regardless of the Windows console code page.  Without
    # this, non-ASCII memory text can be encoded as the local ANSI code page and
    # fail in clients that correctly decode protocol messages as UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")
    try:
        gateway = ReadonlyGateway.from_environment()
    except GatewayConfigError as exc:
        print(f"Echo Pact MCP configuration error: {exc}", file=sys.stderr)
        return 2
    return StdioMcpServer(gateway).run()


if __name__ == "__main__":
    raise SystemExit(main())
