"""Room Ferry full-backup v1 adapter for Echo Pact.

This module is deliberately source-specific.  It validates a single UTF-8
``liora-elion-room-ferry-backup`` JSON file, performs a non-writing dry-run,
and converts eligible input to the source-neutral ``echo-pact-records-v1``
package consumed by the M1 importer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import quote


FERRY_FORMAT = "liora-elion-room-ferry-backup"
FERRY_FORMAT_VERSION = 1
FERRY_SCHEMA_VERSION = 1
ADAPTER_ID = "room-ferry-backup-v1"
RECORD_SCHEMA_VERSION = "echo-pact-records-v1"
MAX_INPUT_BYTES = 500 * 1024 * 1024

TOP_LEVEL_FIELDS = {
    "format",
    "formatVersion",
    "appVersion",
    "schemaVersion",
    "exportedAt",
    "checksumAlgorithm",
    "checksum",
    "data",
}
DATA_FIELDS = {
    "conversations",
    "messages",
    "importBatches",
    "handoffDrafts",
    "appMeta",
}
KNOWN_CONTENT_PART_TYPES = {
    "text",
    "image-placeholder",
    "audio-placeholder",
    "audio-transcription",
    "attachment-placeholder",
    "unknown",
}
VALID_ROLES = {"user", "assistant", "system", "tool"}


class FerryAdapterError(ValueError):
    """The Ferry backup is ineligible for a safe formal conversion."""

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
class BranchPath:
    branch_id: str
    messages: List[Dict[str, Any]]


@dataclass
class FerryInspection:
    report: Dict[str, Any]
    payload: Optional[Dict[str, Any]]
    branch_paths: Dict[str, List[BranchPath]]
    rendered_content: Dict[str, str]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _js_number(value: float) -> str:
    """Serialize the finite JSON numbers used by Ferry like JSON.stringify.

    Ferry-generated numeric fields are safe integers.  The extra formatting
    below keeps ordinary finite decimal appMeta values compatible as well.
    """

    if not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid Ferry JSON")
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    text = repr(value).lower()
    if "e" not in text:
        return text
    mantissa, exponent_text = text.split("e", 1)
    exponent = int(exponent_text)
    sign = ""
    if mantissa.startswith("-"):
        sign = "-"
        mantissa = mantissa[1:]
    digits = mantissa.replace(".", "")
    decimal_position = 1 + exponent
    absolute = abs(value)
    if 1e-6 <= absolute < 1e21:
        if decimal_position <= 0:
            return sign + "0." + ("0" * -decimal_position) + digits
        if decimal_position >= len(digits):
            return sign + digits + ("0" * (decimal_position - len(digits)))
        return sign + digits[:decimal_position] + "." + digits[decimal_position:]
    coefficient = digits[0]
    if len(digits) > 1:
        coefficient += "." + digits[1:].rstrip("0")
        coefficient = coefficient.rstrip(".")
    exponent_sign = "+" if exponent >= 0 else ""
    return f"{sign}{coefficient}e{exponent_sign}{exponent}"


def js_json_stringify(value: Any) -> str:
    """Serialize parsed Ferry data with the JSON.stringify field order rules."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _js_number(value)
    if isinstance(value, list):
        return "[" + ",".join(js_json_stringify(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{_json_string(str(key))}:{js_json_stringify(item)}"
            for key, item in value.items()
        ) + "}"
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def ferry_checksum(data: Mapping[str, Any]) -> str:
    """Return SHA-256(JSON.stringify(data)) using UTF-8 bytes."""

    return _sha256_text(js_json_stringify(dict(data)))


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _as_epoch_ms(value: Any) -> Optional[int]:
    if not _is_number(value):
        return None
    rounded = int(value)
    if float(value) != float(rounded):
        return None
    return rounded


def _timestamp_iso(epoch_ms: int) -> str:
    try:
        result = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("timestamp is outside the supported UTC range") from exc
    return result.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_json_bytes(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FerryAdapterError("Room Ferry backup must be strict UTF-8") from exc

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    try:
        return json.loads(text, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise FerryAdapterError("Room Ferry backup is malformed JSON") from exc


def _load_input_bytes(path: str) -> tuple[bytes, str]:
    input_path = Path(path)
    if not input_path.is_file():
        raise FerryAdapterError(f"Room Ferry backup does not exist: {input_path}")
    size = input_path.stat().st_size
    if size <= 0:
        raise FerryAdapterError("Room Ferry backup is empty")
    if size > MAX_INPUT_BYTES:
        raise FerryAdapterError("Room Ferry backup exceeds the 500 MB safety limit")
    raw = input_path.read_bytes()
    return raw, _sha256_bytes(raw)


def _blank_report(input_sha256: Optional[str] = None) -> Dict[str, Any]:
    return {
        "adapter": ADAPTER_ID,
        "dry_run": True,
        "input_sha256": input_sha256,
        "format": None,
        "format_version": None,
        "app_version": None,
        "schema_version": None,
        "exported_at": None,
        "checksum": {"algorithm": None, "declared": None, "actual": None, "valid": False},
        "counts": {
            "conversations": 0,
            "messages": 0,
            "import_batches": 0,
            "handoff_drafts": 0,
            "app_meta": 0,
        },
        "branching": {
            "single_branch_conversations": 0,
            "multi_branch_conversations": 0,
            "recoverable_multi_branch_conversations": 0,
            "unrecoverable_multi_branch_conversations": 0,
            "derived_branch_paths": 0,
        },
        "missing": {
            "original_message_time": 0,
            "source_message_id": 0,
            "role": 0,
        },
        "content": {
            "empty_messages": 0,
            "source_unknown_parts": 0,
            "unrecognized_part_types": 0,
        },
        "data_policy": {
            "conversations": "convert",
            "messages": "convert",
            "importBatches": "audit-only",
            "handoffDrafts": "count-only",
            "appMeta": "metadata-only",
        },
        "estimated": {
            "output_records": 0,
            "skipped_messages": 0,
            "warnings": 0,
            "fatal": 0,
        },
        "warnings": [],
        "fatal": [],
        "can_convert": False,
    }


def _finish_report(report: Dict[str, Any], issues: _Issues) -> None:
    report["warnings"] = issues.warnings
    report["fatal"] = issues.fatal
    report["estimated"]["warnings"] = sum(issue["count"] for issue in issues.warnings)
    report["estimated"]["fatal"] = sum(issue["count"] for issue in issues.fatal)
    report["can_convert"] = len(issues.fatal) == 0


def _validate_id_array(
    data: Mapping[str, Any], key: str, issues: _Issues
) -> Optional[List[Dict[str, Any]]]:
    value = data.get(key)
    if not isinstance(value, list):
        issues.fail(f"invalid-{key}-array", f"data.{key} must be an array")
        return None
    invalid = sum(
        1
        for item in value
        if not _is_record(item)
        or not isinstance(item.get("id"), str)
        or not item["id"]
    )
    if invalid:
        issues.fail(f"invalid-{key}-id", f"data.{key} contains invalid stable IDs", invalid)
    return [item for item in value if _is_record(item)]


def _validate_app_meta(data: Mapping[str, Any], issues: _Issues) -> Optional[List[Dict[str, Any]]]:
    value = data.get("appMeta")
    if not isinstance(value, list):
        issues.fail("invalid-appMeta-array", "data.appMeta must be an array")
        return None
    invalid = sum(
        1 for item in value
        if not _is_record(item) or not isinstance(item.get("key"), str)
    )
    if invalid:
        issues.fail("invalid-appMeta-key", "data.appMeta contains invalid keys", invalid)
    return [item for item in value if _is_record(item)]


def _render_parts(
    message: Mapping[str, Any], issues: _Issues
) -> Optional[str]:
    parts = message.get("contentParts")
    if not isinstance(parts, list):
        issues.fail("invalid-content-parts", "message contentParts must be an array")
        return None
    rendered: List[str] = []
    for part in parts:
        if not _is_record(part) or not isinstance(part.get("type"), str):
            issues.fail("invalid-content-part", "content part must have a string type")
            continue
        part_type = part["type"]
        if part_type not in KNOWN_CONTENT_PART_TYPES:
            issues.fail(
                "unrecognized-content-part-type",
                "backup contains content part types unknown to Ferry v1",
            )
            continue
        if part_type == "text":
            if not isinstance(part.get("text"), str):
                issues.fail("invalid-text-part", "text content part is missing text")
            else:
                rendered.append(part["text"])
        elif part_type == "image-placeholder":
            rendered.append("[图片]")
        elif part_type == "audio-placeholder":
            rendered.append("[语音]")
        elif part_type == "audio-transcription":
            if not isinstance(part.get("text"), str):
                issues.fail(
                    "invalid-audio-transcription",
                    "audio transcription content part is missing text",
                )
            else:
                rendered.append(f"[语音转写]\n{part['text']}")
        elif part_type == "attachment-placeholder":
            name = part.get("name")
            if name is not None and not isinstance(name, str):
                issues.fail("invalid-attachment-name", "attachment name must be text")
            else:
                rendered.append(f"[附件{'：' + name if name else ''}]")
        else:
            summary = part.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                issues.fail("invalid-unknown-summary", "unknown content part needs a summary")
            else:
                rendered.append(f"[{summary}]")
                issues.warning(
                    "source-unknown-content-part",
                    "Ferry preserved a source part as an explicit unknown summary",
                )
    content = "\n".join(rendered)
    if not content.strip():
        issues.warning("empty-message-skipped", "empty Ferry messages will be skipped")
        return ""
    plain_text = message.get("plainText")
    if not isinstance(plain_text, str):
        issues.fail("missing-plain-text", "message plainText must be a string")
    elif plain_text != content:
        issues.warning(
            "plain-text-rebuilt",
            "plainText differed from contentParts; adapter uses controlled contentParts",
        )
    return content


def _stable_messages(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(messages, key=lambda message: (message.get("sortKey", ""), message.get("id", "")))


def _branch_id(conversation_id: str, decisions: Sequence[str]) -> str:
    decision_text = "\n".join(decisions) if decisions else "single"
    digest = _sha256_text(f"{conversation_id}\n{decision_text}")[:24]
    return f"rfv1_branch_{digest}"


def _single_branch_paths(
    conversation_id: str,
    messages: List[Dict[str, Any]],
    issues: _Issues,
) -> List[BranchPath]:
    if messages and all(
        isinstance(message.get("sourceMessageId"), str)
        and bool(message.get("sourceMessageId"))
        for message in messages
    ):
        # Prefer Ferry's preserved parent graph whenever it is complete.  The
        # sortKey fallback below exists only for source types without IDs.
        return _tree_branch_paths(conversation_id, messages, 0, issues)

    sort_keys = [message.get("sortKey") for message in messages]
    missing_sort = sum(1 for value in sort_keys if not isinstance(value, str) or not value)
    if missing_sort:
        issues.fail("missing-sort-key", "single-branch message order needs stable sortKey", missing_sort)
        return []
    duplicates = len(sort_keys) - len(set(sort_keys))
    if duplicates:
        issues.fail("duplicate-sort-key", "single-branch message order is ambiguous", duplicates)
        return []
    return [BranchPath(_branch_id(conversation_id, []), _stable_messages(messages))]


def _tree_branch_paths(
    conversation_id: str,
    messages: List[Dict[str, Any]],
    declared_branch_count: int,
    issues: _Issues,
) -> List[BranchPath]:
    source_ids: Dict[str, Dict[str, Any]] = {}
    missing_source_ids = 0
    duplicate_source_ids = 0
    for message in messages:
        source_id = message.get("sourceMessageId")
        if not isinstance(source_id, str) or not source_id:
            missing_source_ids += 1
        elif source_id in source_ids:
            duplicate_source_ids += 1
        else:
            source_ids[source_id] = message
    if missing_source_ids:
        issues.fail(
            "multi-branch-missing-source-id",
            "multi-branch topology requires every message sourceMessageId",
            missing_source_ids,
        )
    if duplicate_source_ids:
        issues.fail(
            "branch-tree-duplicate-source-id",
            "branch topology requires unique sourceMessageId values",
            duplicate_source_ids,
        )
    if missing_source_ids or duplicate_source_ids:
        return []

    children: Dict[str, List[Dict[str, Any]]] = {source_id: [] for source_id in source_ids}
    roots: List[Dict[str, Any]] = []
    root_anchors: set[Optional[str]] = set()
    missing_parents = 0
    invalid_parents = 0
    for message in messages:
        parent_id = message.get("parentSourceMessageId")
        if parent_id is None or parent_id == "":
            roots.append(message)
            root_anchors.add(None)
        elif not isinstance(parent_id, str):
            invalid_parents += 1
        elif parent_id not in source_ids:
            missing_parents += 1
            roots.append(message)
            root_anchors.add(parent_id)
        else:
            children[parent_id].append(message)
    if invalid_parents:
        issues.fail(
            "branch-tree-invalid-parent",
            "branch parentSourceMessageId must be text when present",
            invalid_parents,
        )
        return []
    if missing_parents:
        issues.warning(
            "compressed-missing-structural-parent",
            "stored messages share an omitted structural parent with no content",
            missing_parents,
        )
    if len(root_anchors) > 1:
        issues.fail(
            "branch-tree-ambiguous-root-components",
            "branch topology has multiple unconnected root anchors",
            len(root_anchors),
        )
        return []
    if not roots and messages:
        issues.fail("branch-tree-no-root", "branch graph has no root")
        return []

    for source_id in children:
        children[source_id] = _stable_messages(children[source_id])
    roots = _stable_messages(roots)
    computed_branch_count = max(0, len(roots) - 1) + sum(
        max(0, len(items) - 1) for items in children.values()
    )
    if computed_branch_count != declared_branch_count:
        issues.warning(
            "stale-branch-count-metadata",
            "declared branchCount differs from the recoverable parent topology",
        )

    parent_by_source_id: Dict[str, Optional[str]] = {}
    for source_id, message in source_ids.items():
        parent_id = message.get("parentSourceMessageId")
        parent_by_source_id[source_id] = (
            parent_id
            if isinstance(parent_id, str) and parent_id in source_ids
            else None
        )

    # Follow parent pointers iteratively. Real archives can contain conversations
    # longer than Python's recursion limit, so tree validation must not recurse.
    state: Dict[str, int] = {}
    for start in source_ids:
        if state.get(start) == 2:
            continue
        trail: List[str] = []
        current: Optional[str] = start
        while current is not None and state.get(current, 0) == 0:
            state[current] = 1
            trail.append(current)
            current = parent_by_source_id[current]
        if current is not None and state.get(current) == 1:
            issues.fail("branch-tree-cycle", "branch topology contains a cycle")
            return []
        for source_id in trail:
            state[source_id] = 2

    visited_nodes: set[str] = set()
    pending = [root["sourceMessageId"] for root in roots]
    while pending:
        source_id = pending.pop()
        if source_id in visited_nodes:
            continue
        visited_nodes.add(source_id)
        pending.extend(child["sourceMessageId"] for child in children[source_id])
    if len(visited_nodes) != len(messages):
        issues.fail(
            "branch-tree-unreachable-message",
            "branch topology contains unreachable messages",
            len(messages) - len(visited_nodes),
        )
        return []

    paths: List[BranchPath] = []
    leaves = _stable_messages(
        message
        for source_id, message in source_ids.items()
        if not children[source_id]
    )
    for leaf in leaves:
        reversed_path: List[Dict[str, Any]] = []
        current = leaf["sourceMessageId"]
        while current is not None:
            reversed_path.append(source_ids[current])
            current = parent_by_source_id[current]
        path = list(reversed(reversed_path))
        decisions: List[str] = []
        if len(roots) > 1:
            decisions.append(path[0]["id"])
        for message in path[1:]:
            parent_id = message["parentSourceMessageId"]
            if len(children[parent_id]) > 1:
                decisions.append(message["id"])
        paths.append(BranchPath(_branch_id(conversation_id, decisions), path))

    unique_branch_ids = {path.branch_id for path in paths}
    if len(unique_branch_ids) != len(paths):
        issues.fail("derived-branch-id-collision", "derived branch IDs are not unique")
        return []
    return sorted(paths, key=lambda path: path.branch_id)


def inspect_ferry_backup(path: str) -> FerryInspection:
    """Perform a content-redacted dry-run.  No database or output is written."""

    issues = _Issues()
    try:
        raw, input_sha256 = _load_input_bytes(path)
    except FerryAdapterError as exc:
        report = _blank_report()
        issues.fail("input-unreadable", str(exc))
        _finish_report(report, issues)
        return FerryInspection(report, None, {}, {})

    report = _blank_report(input_sha256)
    report["input_bytes"] = len(raw)
    try:
        parsed = _parse_json_bytes(raw)
    except FerryAdapterError as exc:
        issues.fail("malformed-json", str(exc))
        _finish_report(report, issues)
        return FerryInspection(report, None, {}, {})
    if not _is_record(parsed):
        issues.fail("invalid-envelope", "backup root must be a JSON object")
        _finish_report(report, issues)
        return FerryInspection(report, None, {}, {})

    payload: Dict[str, Any] = parsed
    report["format"] = payload.get("format")
    report["format_version"] = payload.get("formatVersion")
    report["app_version"] = payload.get("appVersion")
    report["schema_version"] = payload.get("schemaVersion")
    report["exported_at"] = payload.get("exportedAt")
    report["checksum"] = {
        "algorithm": payload.get("checksumAlgorithm"),
        "declared": payload.get("checksum"),
        "actual": None,
        "valid": False,
    }

    unknown_top = set(payload) - TOP_LEVEL_FIELDS
    if unknown_top:
        issues.warning(
            "unknown-top-level-field",
            "backup contains unrecognized top-level fields",
            len(unknown_top),
        )
    if payload.get("format") != FERRY_FORMAT:
        issues.fail("unsupported-format", "backup format is not Room Ferry full backup")
    if payload.get("formatVersion") != FERRY_FORMAT_VERSION:
        issues.fail("unsupported-format-version", "backup formatVersion is not supported")
    if payload.get("schemaVersion") != FERRY_SCHEMA_VERSION:
        issues.fail("unsupported-schema-version", "backup schemaVersion is not supported")
    if not isinstance(payload.get("appVersion"), str) or not payload["appVersion"]:
        issues.fail("invalid-app-version", "backup appVersion must be non-empty text")
    exported_at = _as_epoch_ms(payload.get("exportedAt"))
    if exported_at is None:
        issues.fail("invalid-exported-at", "backup exportedAt must be epoch milliseconds")
    else:
        try:
            report["exported_at"] = _timestamp_iso(exported_at)
        except ValueError:
            issues.fail("invalid-exported-at", "backup exportedAt is outside the UTC range")
    if payload.get("checksumAlgorithm") != "SHA-256":
        issues.fail("unsupported-checksum-algorithm", "checksumAlgorithm must be SHA-256")
    declared_checksum = payload.get("checksum")
    if (
        not isinstance(declared_checksum, str)
        or len(declared_checksum) != 64
        or any(character not in "0123456789abcdef" for character in declared_checksum.lower())
    ):
        issues.fail("invalid-checksum", "backup checksum must be 64 hexadecimal characters")

    data = payload.get("data")
    if not _is_record(data):
        issues.fail("invalid-data", "backup data must be an object")
        _finish_report(report, issues)
        return FerryInspection(report, payload, {}, {})
    unknown_data = set(data) - DATA_FIELDS
    if unknown_data:
        issues.warning(
            "unknown-data-field",
            "backup data contains unrecognized fields covered by checksum",
            len(unknown_data),
        )

    conversations = _validate_id_array(data, "conversations", issues)
    messages = _validate_id_array(data, "messages", issues)
    import_batches = _validate_id_array(data, "importBatches", issues)
    handoff_drafts = _validate_id_array(data, "handoffDrafts", issues)
    app_meta = _validate_app_meta(data, issues)
    for report_key, values in (
        ("conversations", conversations),
        ("messages", messages),
        ("import_batches", import_batches),
        ("handoff_drafts", handoff_drafts),
        ("app_meta", app_meta),
    ):
        if values is not None:
            report["counts"][report_key] = len(values)

    if not issues.fatal:
        try:
            actual_checksum = ferry_checksum(data)
            report["checksum"]["actual"] = actual_checksum
            report["checksum"]["valid"] = (
                actual_checksum == declared_checksum
            )
            if not report["checksum"]["valid"]:
                issues.fail("checksum-mismatch", "backup data checksum does not match")
        except (TypeError, ValueError):
            issues.fail("checksum-serialization-failed", "backup data cannot be checksummed safely")

    branch_paths: Dict[str, List[BranchPath]] = {}
    rendered_content: Dict[str, str] = {}
    if issues.fatal or conversations is None or messages is None:
        _finish_report(report, issues)
        return FerryInspection(report, payload, branch_paths, rendered_content)

    conversation_ids: Dict[str, Dict[str, Any]] = {}
    duplicate_conversations = 0
    for conversation in conversations:
        conversation_id = conversation.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            continue
        if conversation_id in conversation_ids:
            duplicate_conversations += 1
        else:
            conversation_ids[conversation_id] = conversation
    if duplicate_conversations:
        issues.fail(
            "duplicate-conversation-id",
            "backup contains duplicate Ferry conversation IDs",
            duplicate_conversations,
        )

    messages_by_conversation: Dict[str, List[Dict[str, Any]]] = {
        conversation_id: [] for conversation_id in conversation_ids
    }
    message_ids: set[str] = set()
    duplicate_messages = 0
    orphan_messages = 0
    missing_source_message_ids = 0
    missing_times = 0
    missing_roles = 0
    empty_messages = 0
    for message in messages:
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            continue
        if message_id in message_ids:
            duplicate_messages += 1
        message_ids.add(message_id)
        conversation_id = message.get("conversationId")
        if not isinstance(conversation_id, str) or conversation_id not in messages_by_conversation:
            orphan_messages += 1
            continue
        messages_by_conversation[conversation_id].append(message)
        if not isinstance(message.get("sourceMessageId"), str) or not message.get("sourceMessageId"):
            missing_source_message_ids += 1
        created_at = _as_epoch_ms(message.get("createdAt"))
        if created_at is None:
            missing_times += 1
        else:
            try:
                _timestamp_iso(created_at)
            except ValueError:
                missing_times += 1
        role = message.get("role")
        if role not in VALID_ROLES:
            missing_roles += 1
        content = _render_parts(message, issues)
        if content == "":
            empty_messages += 1
        elif content is not None:
            rendered_content[message_id] = content

    if duplicate_messages:
        issues.fail("duplicate-message-id", "backup contains duplicate Ferry message IDs", duplicate_messages)
    if orphan_messages:
        issues.fail("orphan-message", "messages reference unknown Ferry conversations", orphan_messages)
    if missing_times:
        issues.fail(
            "missing-original-message-time",
            "messages without createdAt cannot satisfy records-v1 created_at",
            missing_times,
        )
    if missing_roles:
        issues.fail(
            "missing-or-unknown-role",
            "messages need a role supported by records-v1",
            missing_roles,
        )
    if missing_source_message_ids:
        issues.warning(
            "missing-source-message-id",
            "Ferry sourceMessageId is absent; Ferry message ID remains available",
            missing_source_message_ids,
        )
    report["missing"]["original_message_time"] = missing_times
    report["missing"]["source_message_id"] = missing_source_message_ids
    report["missing"]["role"] = missing_roles
    report["content"]["empty_messages"] = empty_messages
    report["content"]["source_unknown_parts"] = sum(
        issue["count"] for issue in issues.warnings
        if issue["code"] == "source-unknown-content-part"
    )
    report["content"]["unrecognized_part_types"] = sum(
        issue["count"] for issue in issues.fatal
        if issue["code"] == "unrecognized-content-part-type"
    )

    if app_meta is not None:
        schema_values = [item.get("value") for item in app_meta if item.get("key") == "schemaVersion"]
        report["app_meta_schema_version"] = schema_values[-1] if schema_values else None
        if schema_values and schema_values[-1] != payload.get("schemaVersion"):
            issues.fail(
                "app-meta-schema-mismatch",
                "appMeta schemaVersion conflicts with backup header",
            )

    for conversation_id in sorted(conversation_ids):
        conversation = conversation_ids[conversation_id]
        conversation_messages = messages_by_conversation[conversation_id]
        declared_message_count = conversation.get("messageCount")
        if declared_message_count != len(conversation_messages):
            issues.fail(
                "conversation-message-count-mismatch",
                "conversation messageCount does not match contained messages",
            )
            continue
        branch_count = conversation.get("branchCount")
        if not isinstance(branch_count, int) or isinstance(branch_count, bool) or branch_count < 0:
            issues.fail("invalid-branch-count", "conversation branchCount must be a non-negative integer")
            continue
        fatal_before = sum(issue["count"] for issue in issues.fatal)
        complete_source_ids = all(
            isinstance(message.get("sourceMessageId"), str)
            and bool(message.get("sourceMessageId"))
            for message in conversation_messages
        )
        if complete_source_ids or branch_count > 0:
            paths = _tree_branch_paths(
                conversation_id,
                conversation_messages,
                branch_count,
                issues,
            )
        else:
            paths = _single_branch_paths(
                conversation_id, conversation_messages, issues
            )
        fatal_after = sum(issue["count"] for issue in issues.fatal)
        if paths and fatal_after == fatal_before:
            if len(paths) > 1:
                report["branching"]["multi_branch_conversations"] += 1
                report["branching"]["recoverable_multi_branch_conversations"] += 1
            else:
                report["branching"]["single_branch_conversations"] += 1
        elif branch_count > 0:
            report["branching"]["multi_branch_conversations"] += 1
            report["branching"]["unrecoverable_multi_branch_conversations"] += 1
        else:
            report["branching"]["single_branch_conversations"] += 1
        if paths:
            branch_paths[conversation_id] = paths
            report["branching"]["derived_branch_paths"] += len(paths)

    report["estimated"]["output_records"] = sum(
        1
        for paths in branch_paths.values()
        for path in paths
        for message in path.messages
        if message.get("id") in rendered_content
    )
    report["estimated"]["skipped_messages"] = empty_messages
    _finish_report(report, issues)
    return FerryInspection(report, payload, branch_paths, rendered_content)


def dry_run_ferry_backup(path: str) -> Dict[str, Any]:
    return inspect_ferry_backup(path).report


def _source_ref(input_sha256: str, conversation_id: str, message_id: str) -> str:
    return (
        f"room-ferry-backup-v1://sha256/{input_sha256}"
        f"/conversation/{quote(conversation_id, safe='')}"
        f"/message/{quote(message_id, safe='')}"
    )


def convert_ferry_backup(path: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a deterministic records-v1 package and its redacted dry-run."""

    inspection = inspect_ferry_backup(path)
    report = inspection.report
    if not report["can_convert"] or inspection.payload is None:
        raise FerryAdapterError("Room Ferry backup failed dry-run", report=report)
    payload = inspection.payload
    exported_at = _as_epoch_ms(payload["exportedAt"])
    if exported_at is None:
        raise FerryAdapterError("Room Ferry exportedAt became invalid", report=report)
    source_cutoff_at = _timestamp_iso(exported_at)
    input_sha256 = report["input_sha256"]
    records: List[Dict[str, Any]] = []

    for conversation_id in sorted(inspection.branch_paths):
        paths = inspection.branch_paths[conversation_id]
        for branch in sorted(paths, key=lambda item: item.branch_id):
            for message in branch.messages:
                ferry_message_id = message["id"]
                content = inspection.rendered_content.get(ferry_message_id)
                if content is None:
                    continue
                created_at = _as_epoch_ms(message.get("createdAt"))
                if created_at is None:
                    raise FerryAdapterError("Room Ferry message time became invalid", report=report)
                record_seed = f"{conversation_id}\n{branch.branch_id}\n{ferry_message_id}"
                records.append(
                    {
                        "record_id": f"rfv1_{_sha256_text(record_seed)}",
                        "source_kind": ADAPTER_ID,
                        "source_ref": _source_ref(input_sha256, conversation_id, ferry_message_id),
                        "conversation_id": f"room-ferry-v1:{conversation_id}",
                        "branch_id": branch.branch_id,
                        "message_id": f"room-ferry-v1:{ferry_message_id}",
                        "role": message["role"],
                        "content": content,
                        "created_at": _timestamp_iso(created_at),
                        "verified": False,
                        "authority": "room-ferry-unverified-archive",
                        "source_cutoff_at": source_cutoff_at,
                        "conflict_group_id": None,
                    }
                )

    package = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "source": {
            "adapter": ADAPTER_ID,
            "input_sha256": input_sha256,
            "format": payload["format"],
            "format_version": payload["formatVersion"],
            "app_version": payload["appVersion"],
            "schema_version": payload["schemaVersion"],
            "exported_at": source_cutoff_at,
            "verification": "unverified-archive",
        },
        "records": records,
    }
    if len(records) != report["estimated"]["output_records"]:
        raise FerryAdapterError("conversion record count differed from dry-run", report=report)
    return package, report


def serialize_record_package(package: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(package, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def write_converted_package(path: str, output_path: str) -> Dict[str, Any]:
    """Atomically create one formal records-v1 package without overwriting."""

    destination = Path(output_path)
    if destination.exists():
        raise FileExistsError(f"conversion output already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"conversion output parent does not exist: {destination.parent}"
        )
    package, report = convert_ferry_backup(path)
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
