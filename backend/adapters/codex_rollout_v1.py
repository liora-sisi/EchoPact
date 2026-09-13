"""Codex app-server thread snapshot adapter for Echo Pact.

The adapter is deliberately source-specific.  It accepts a checksummed,
offline snapshot made from ``thread/read(includeTurns=true)`` responses,
keeps only the visible user/assistant conversation, and emits the
source-neutral compact records package consumed by the existing importer.

No Codex state or Echo Pact database is opened by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote, urlencode


CODEX_ROLLOUT_FORMAT = "echo-pact-codex-rollout"
CODEX_ROLLOUT_FORMAT_VERSION = 1
SOURCE_METHOD = "thread/read"
ADAPTER_ID = "codex-app-server-v1"
RECORD_SCHEMA_VERSION = "echo-pact-records-v2"
MAX_INPUT_BYTES = 500 * 1024 * 1024

TOP_LEVEL_FIELDS = {
    "format",
    "formatVersion",
    "capturedAt",
    "codexCliVersion",
    "sourceMethod",
    "checksumAlgorithm",
    "checksum",
    "threads",
}
TERMINAL_TURN_STATUSES = {"completed", "interrupted", "failed"}
MESSAGE_ITEM_TYPES = {"userMessage", "agentMessage"}
AGENT_PHASES = {None, "commentary", "final_answer"}
USER_INPUT_PLACEHOLDERS = {
    "image": "[Codex image input]",
    "localImage": "[Codex local image input]",
    "audio": "[Codex audio input]",
    "localAudio": "[Codex local audio input]",
    "skill": "[Codex skill input]",
    "mention": "[Codex mention input]",
}


class CodexRolloutAdapterError(ValueError):
    """The snapshot is ineligible for a safe formal conversion."""

    def __init__(self, message: str, *, report: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.report = report


class _Issues:
    def __init__(self) -> None:
        self.warnings: List[Dict[str, Any]] = []
        self.fatal: List[Dict[str, Any]] = []

    @staticmethod
    def _add(target: List[Dict[str, Any]], code: str, message: str, count: int) -> None:
        for issue in target:
            if issue["code"] == code:
                issue["count"] += count
                return
        target.append({"code": code, "message": message, "count": count})

    def warning(self, code: str, message: str, count: int = 1) -> None:
        self._add(self.warnings, code, message, count)

    def fail(self, code: str, message: str, count: int = 1) -> None:
        self._add(self.fatal, code, message, count)


@dataclass(frozen=True)
class CodexMessage:
    thread_id: str
    turn_id: str
    item_id: str
    role: str
    content: str
    created_at: str
    branch_id: str
    position: int
    phase: Optional[str] = None


@dataclass
class CodexRolloutInspection:
    report: Dict[str, Any]
    payload: Optional[Dict[str, Any]]
    messages: List[CodexMessage]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def codex_rollout_checksum(threads: Sequence[Mapping[str, Any]]) -> str:
    """Return the snapshot checksum over the canonical ``threads`` array."""

    return _sha256_bytes(_canonical_json(list(threads)))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_string(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _timestamp_from_epoch_seconds(value: Any) -> Optional[str]:
    if not _is_int(value):
        return None
    try:
        result = datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return result.isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalized_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _blank_report(input_sha256: Optional[str] = None) -> Dict[str, Any]:
    return {
        "adapter": ADAPTER_ID,
        "dry_run": True,
        "input_sha256": input_sha256,
        "format": None,
        "format_version": None,
        "captured_at": None,
        "codex_cli_version": None,
        "source_method": None,
        "checksum": {
            "algorithm": None,
            "declared": None,
            "actual": None,
            "valid": False,
        },
        "counts": {
            "threads": 0,
            "turns": 0,
            "items": 0,
            "user_messages": 0,
            "agent_messages": 0,
            "ignored_nonconversation_items": 0,
        },
        "estimated": {
            "output_records": 0,
            "skipped_empty_messages": 0,
            "fatal": 0,
            "warnings": 0,
        },
        "privacy": {
            "message_text_reported": False,
            "cwd_reported": False,
            "media_locations_preserved": False,
            "tool_and_reasoning_items_imported": False,
        },
        "warnings": [],
        "fatal": [],
        "can_convert": False,
    }


def _finish_report(report: Dict[str, Any], issues: _Issues) -> None:
    report["warnings"] = issues.warnings
    report["fatal"] = issues.fatal
    report["estimated"]["warnings"] = sum(
        issue["count"] for issue in issues.warnings
    )
    report["estimated"]["fatal"] = sum(issue["count"] for issue in issues.fatal)
    report["can_convert"] = not issues.fatal


def _load_json(path: str, report: Dict[str, Any], issues: _Issues) -> Optional[Any]:
    input_path = Path(path)
    if not input_path.is_file():
        issues.fail("input-not-found", "Codex rollout snapshot does not exist")
        return None
    try:
        size = input_path.stat().st_size
    except OSError:
        issues.fail("input-unreadable", "Codex rollout snapshot cannot be inspected")
        return None
    if size <= 0:
        issues.fail("input-empty", "Codex rollout snapshot is empty")
        return None
    if size > MAX_INPUT_BYTES:
        issues.fail("input-too-large", "Codex rollout snapshot exceeds 500 MB")
        return None
    try:
        raw = input_path.read_bytes()
    except OSError:
        issues.fail("input-unreadable", "Codex rollout snapshot cannot be read")
        return None
    report["input_sha256"] = _sha256_bytes(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        issues.fail("input-not-utf8", "Codex rollout snapshot must be strict UTF-8")
        return None

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    try:
        return json.loads(text, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError):
        issues.fail("malformed-json", "Codex rollout snapshot is malformed JSON")
        return None


def _render_user_content(
    value: Any,
    issues: _Issues,
) -> Optional[str]:
    if not isinstance(value, list):
        issues.fail(
            "invalid-user-content",
            "Codex userMessage content must be an array",
        )
        return None
    rendered: List[str] = []
    for part in value:
        if not isinstance(part, dict):
            issues.fail(
                "invalid-user-input-part",
                "Codex user input parts must be objects",
            )
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str):
                issues.fail(
                    "invalid-user-text",
                    "Codex text input must contain a string",
                )
                continue
            rendered.append(text)
            continue
        placeholder = USER_INPUT_PLACEHOLDERS.get(part_type)
        if placeholder is None:
            issues.fail(
                "unrecognized-user-input-type",
                "A Codex user input type has not been reviewed",
            )
            continue
        # Deliberately do not preserve a URL, local path, skill path, or mention path.
        rendered.append(placeholder)
    return "\n".join(part for part in rendered if part != "")


def _turn_order(turn: Mapping[str, Any]) -> tuple[int, int, str]:
    started_at = turn.get("startedAt")
    return (
        1 if not _is_int(started_at) else 0,
        started_at if _is_int(started_at) else 0,
        str(turn.get("id", "")),
    )


def _branch_id(thread_id: str) -> str:
    return f"codex_thread_{_sha256_text(thread_id)[:24]}"


def inspect_codex_rollout(path: str) -> CodexRolloutInspection:
    """Inspect one snapshot and return a redacted, non-writing report."""

    issues = _Issues()
    report = _blank_report()
    root = _load_json(path, report, issues)
    if root is None:
        _finish_report(report, issues)
        return CodexRolloutInspection(report, None, [])
    if not isinstance(root, dict):
        issues.fail("invalid-envelope", "Codex rollout snapshot must be an object")
        _finish_report(report, issues)
        return CodexRolloutInspection(report, None, [])

    unknown_fields = set(root) - TOP_LEVEL_FIELDS
    if unknown_fields:
        issues.warning(
            "unknown-top-level-field",
            "Unreviewed top-level fields are ignored",
            len(unknown_fields),
        )

    report["format"] = root.get("format")
    report["format_version"] = root.get("formatVersion")
    report["codex_cli_version"] = root.get("codexCliVersion")
    report["source_method"] = root.get("sourceMethod")
    report["checksum"]["algorithm"] = root.get("checksumAlgorithm")
    report["checksum"]["declared"] = root.get("checksum")

    if root.get("format") != CODEX_ROLLOUT_FORMAT:
        issues.fail("unsupported-format", "Unsupported Codex rollout format")
    if root.get("formatVersion") != CODEX_ROLLOUT_FORMAT_VERSION:
        issues.fail(
            "unsupported-format-version",
            "Unsupported Codex rollout format version",
        )
    if _nonempty_string(root.get("codexCliVersion")) is None:
        issues.fail("missing-codex-version", "codexCliVersion is required")
    if root.get("sourceMethod") != SOURCE_METHOD:
        issues.fail(
            "unsupported-source-method",
            "Only thread/read snapshots are supported",
        )

    captured_at = _normalized_timestamp(root.get("capturedAt"))
    report["captured_at"] = captured_at
    if captured_at is None:
        issues.fail(
            "invalid-captured-at",
            "capturedAt must be a timezone-aware ISO-8601 timestamp",
        )

    threads = root.get("threads")
    if not isinstance(threads, list):
        issues.fail("invalid-threads", "threads must be an array")
        threads = []
    report["counts"]["threads"] = len(threads)

    actual_checksum: Optional[str] = None
    try:
        actual_checksum = codex_rollout_checksum(threads)
    except (TypeError, ValueError):
        issues.fail("checksum-input-invalid", "threads cannot be checksummed safely")
    report["checksum"]["actual"] = actual_checksum
    declared_checksum = root.get("checksum")
    if root.get("checksumAlgorithm") != "SHA-256":
        issues.fail("unsupported-checksum", "checksumAlgorithm must be SHA-256")
    if (
        not isinstance(declared_checksum, str)
        or len(declared_checksum) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in declared_checksum)
    ):
        issues.fail("invalid-checksum", "checksum must be 64 hexadecimal characters")
    elif actual_checksum is not None and declared_checksum.lower() != actual_checksum:
        issues.fail("checksum-mismatch", "Codex rollout snapshot checksum does not match")
    else:
        report["checksum"]["valid"] = actual_checksum is not None

    messages: List[CodexMessage] = []
    seen_threads: set[str] = set()
    skipped_empty = 0
    sortable_threads: List[Mapping[str, Any]] = []
    for thread in threads:
        if not isinstance(thread, dict):
            issues.fail("invalid-thread", "Each Codex thread must be an object")
            continue
        sortable_threads.append(thread)

    for thread in sorted(sortable_threads, key=lambda item: str(item.get("id", ""))):
        thread_id = _nonempty_string(thread.get("id"))
        if thread_id is None:
            issues.fail("missing-thread-id", "Each Codex thread requires an id")
            continue
        if thread_id in seen_threads:
            issues.fail("duplicate-thread-id", "Codex thread ids must be unique")
            continue
        seen_threads.add(thread_id)
        if thread.get("ephemeral") is True:
            issues.warning(
                "ephemeral-thread",
                "An ephemeral Codex thread is present in the snapshot",
            )

        turns = thread.get("turns")
        if not isinstance(turns, list):
            issues.fail("invalid-turns", "Each Codex thread requires a turns array")
            continue
        report["counts"]["turns"] += len(turns)
        branch_id = _branch_id(thread_id)
        position = 0
        seen_turns: set[str] = set()
        seen_message_items: set[str] = set()
        sortable_turns: List[Mapping[str, Any]] = []
        for turn in turns:
            if not isinstance(turn, dict):
                issues.fail("invalid-turn", "Each Codex turn must be an object")
                continue
            sortable_turns.append(turn)

        for turn in sorted(sortable_turns, key=_turn_order):
            turn_id = _nonempty_string(turn.get("id"))
            if turn_id is None:
                issues.fail("missing-turn-id", "Each Codex turn requires an id")
                continue
            if turn_id in seen_turns:
                issues.fail("duplicate-turn-id", "Codex turn ids must be unique per thread")
                continue
            seen_turns.add(turn_id)
            items_view = turn.get("itemsView", "full")
            if items_view != "full":
                issues.fail(
                    "incomplete-turn-items",
                    "Codex turns must be captured with full items",
                )
                continue
            status = turn.get("status")
            if status not in TERMINAL_TURN_STATUSES:
                issues.fail(
                    "nonterminal-turn",
                    "In-progress or unknown Codex turns cannot be archived",
                )
                continue
            items = turn.get("items")
            if not isinstance(items, list):
                issues.fail("invalid-items", "Each Codex turn requires an items array")
                continue
            report["counts"]["items"] += len(items)
            started_at = _timestamp_from_epoch_seconds(turn.get("startedAt"))
            completed_at = _timestamp_from_epoch_seconds(turn.get("completedAt"))

            for item in items:
                if not isinstance(item, dict):
                    issues.fail("invalid-item", "Each Codex item must be an object")
                    continue
                item_type = item.get("type")
                if item_type not in MESSAGE_ITEM_TYPES:
                    report["counts"]["ignored_nonconversation_items"] += 1
                    continue
                item_id = _nonempty_string(item.get("id"))
                if item_id is None:
                    issues.fail("missing-item-id", "Conversation items require an id")
                    continue
                if item_id in seen_message_items:
                    issues.fail(
                        "duplicate-message-item-id",
                        "Conversation item ids must be unique per thread",
                    )
                    continue
                seen_message_items.add(item_id)

                if item_type == "userMessage":
                    report["counts"]["user_messages"] += 1
                    if started_at is None:
                        issues.fail(
                            "missing-user-message-time",
                            "A userMessage requires the turn's original startedAt",
                        )
                        continue
                    content = _render_user_content(item.get("content"), issues)
                    role = "user"
                    created_at = started_at
                    phase = None
                else:
                    report["counts"]["agent_messages"] += 1
                    if completed_at is None:
                        issues.fail(
                            "missing-agent-message-time",
                            "An agentMessage requires the turn's original completedAt",
                        )
                        continue
                    text = item.get("text")
                    if not isinstance(text, str):
                        issues.fail(
                            "invalid-agent-message",
                            "Codex agentMessage text must be a string",
                        )
                        continue
                    phase = item.get("phase")
                    if phase not in AGENT_PHASES:
                        issues.fail(
                            "unrecognized-agent-phase",
                            "A Codex agent message phase has not been reviewed",
                        )
                        continue
                    content = text
                    role = "assistant"
                    created_at = completed_at

                if content is None:
                    continue
                if not content.strip():
                    skipped_empty += 1
                    issues.warning(
                        "empty-message-skipped",
                        "An empty Codex conversation message was skipped",
                    )
                    continue
                messages.append(
                    CodexMessage(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        item_id=item_id,
                        role=role,
                        content=content,
                        created_at=created_at,
                        branch_id=branch_id,
                        position=position,
                        phase=phase,
                    )
                )
                position += 1

    report["estimated"]["output_records"] = len(messages)
    report["estimated"]["skipped_empty_messages"] = skipped_empty
    _finish_report(report, issues)
    return CodexRolloutInspection(report, root, messages)


def dry_run_codex_rollout(path: str) -> Dict[str, Any]:
    return inspect_codex_rollout(path).report


def _source_ref(message: CodexMessage) -> str:
    result = (
        f"codex-app-server-v1://thread/{quote(message.thread_id, safe='')}"
        f"/turn/{quote(message.turn_id, safe='')}"
        f"/item/{quote(message.item_id, safe='')}"
    )
    if message.phase is not None:
        result += "?" + urlencode({"phase": message.phase})
    return result


def convert_codex_rollout(path: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a deterministic compact record package and redacted dry-run."""

    inspection = inspect_codex_rollout(path)
    report = inspection.report
    payload = inspection.payload
    if not report["can_convert"] or payload is None:
        raise CodexRolloutAdapterError(
            "Codex rollout snapshot failed dry-run",
            report=report,
        )
    source_cutoff_at = report["captured_at"]
    if source_cutoff_at is None:
        raise CodexRolloutAdapterError(
            "Codex rollout capturedAt became invalid",
            report=report,
        )

    records: List[Dict[str, Any]] = []
    for message in sorted(
        inspection.messages,
        key=lambda item: (item.thread_id, item.position, item.turn_id, item.item_id),
    ):
        record_seed = "\n".join(
            (message.thread_id, message.turn_id, message.item_id)
        )
        record_hash = _sha256_text(record_seed)
        records.append(
            {
                "record_id": f"codexv1_{record_hash}",
                "source_kind": ADAPTER_ID,
                "source_ref": _source_ref(message),
                "conversation_id": f"codex-app-server-v1:{message.thread_id}",
                "branch_memberships": [
                    {"branch_id": message.branch_id, "position": message.position}
                ],
                "message_id": f"codex-app-server-v1:{record_hash}",
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
                "verified": False,
                "authority": "codex-local-unverified-archive",
                "source_cutoff_at": source_cutoff_at,
                "conflict_group_id": None,
            }
        )

    package = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "source": {
            "adapter": ADAPTER_ID,
            "input_sha256": report["input_sha256"],
            "format": payload["format"],
            "format_version": payload["formatVersion"],
            "codex_cli_version": payload["codexCliVersion"],
            "source_method": payload["sourceMethod"],
            "captured_at": source_cutoff_at,
            "verification": "unverified-local-archive",
        },
        "records": records,
    }
    if len(records) != report["estimated"]["output_records"]:
        raise CodexRolloutAdapterError(
            "conversion record count differed from dry-run",
            report=report,
        )
    return package, report


def serialize_record_package(package: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            package,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def write_converted_package(path: str, output_path: str) -> Dict[str, Any]:
    """Atomically create one formal compact package without overwriting."""

    destination = Path(output_path)
    if destination.exists():
        raise FileExistsError(f"conversion output already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"conversion output parent does not exist: {destination.parent}"
        )
    package, report = convert_codex_rollout(path)
    output_bytes = serialize_record_package(package)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary.write(output_bytes)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return {
        "adapter": ADAPTER_ID,
        "output_path": str(destination),
        "output_records": len(package["records"]),
        "output_bytes": len(output_bytes),
        "output_sha256": _sha256_bytes(output_bytes),
        "dry_run": report,
    }
