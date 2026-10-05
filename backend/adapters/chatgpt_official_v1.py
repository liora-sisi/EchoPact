"""ChatGPT official account-export adapter for Echo Pact.

The adapter accepts one ZIP downloaded from ChatGPT's account export. It
validates the export manifest and ZIP CRCs, reads only the logical
``conversations.json`` shards, keeps reviewed human-facing conversation layers, and
emits Echo Pact's source-neutral compact records-v2 package.

The original ZIP is never extracted or modified. Titles, message text, and
source identifiers are deliberately absent from the dry-run report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote, urlencode


ADAPTER_ID = "chatgpt-official-export-v1"
MANIFEST_NAME = "export_manifest.json"
MANIFEST_VERSION = 1
RECORD_SCHEMA_VERSION = "echo-pact-records-v2"
VISIBLE_CONTENT_TYPES = frozenset({"text", "multimodal_text"})
PROCESS_CONTENT_TYPES = frozenset({"thoughts", "reasoning_recap"})
VISIBLE_ROLES = frozenset({"user", "assistant"})
SHARD_RE = re.compile(r"^conversations-(\d+)\.json$")
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
MAX_CONVERSATION_SHARD_BYTES = 512 * 1024 * 1024

_MEDIA_PLACEHOLDERS = {
    "image_asset_pointer": "[ChatGPT image]",
    "audio_asset_pointer": "[ChatGPT audio]",
    "real_time_user_audio_video_asset_pointer": "[ChatGPT realtime audio/video]",
}


class ChatGPTOfficialAdapterError(ValueError):
    """The archive is ineligible for a safe formal conversion."""

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
class OfficialMessage:
    conversation_id: str
    message_id: str
    role: str
    content: str
    created_at: str
    shard_name: str
    content_type: str
    phase: Optional[str]
    branch_memberships: tuple[tuple[str, int], ...]


@dataclass
class OfficialInspection:
    report: Dict[str, Any]
    messages: List[OfficialMessage]


def _blank_report() -> Dict[str, Any]:
    return {
        "adapter": ADAPTER_ID,
        "dry_run": True,
        "archive_sha256": None,
        "archive_bytes": 0,
        "manifest": {
            "version": None,
            "entry_count": 0,
            "declared_file_count": 0,
            "file_set_matches": False,
            "sizes_match": False,
            "zip_crc_ok": False,
        },
        "logical_conversations": {"sharded": None, "shard_count": 0},
        "counts": {
            "conversations": 0,
            "mapping_nodes": 0,
            "visible_user_messages": 0,
            "visible_assistant_messages": 0,
            "thought_messages_retained": 0,
            "reasoning_recap_messages_retained": 0,
            "empty_process_messages_skipped": 0,
            "empty_visible_messages_skipped": 0,
            "media_placeholders": 0,
            "audio_transcriptions": 0,
            "branches": 0,
            "branch_memberships": 0,
        },
        "source_cutoff_at": None,
        "source_cutoff_basis": "latest-conversation-update-or-message-time",
        "estimated_output_records": 0,
        "warnings": [],
        "fatal_issues": [],
        "can_convert": False,
    }


def _finish_report(report: Dict[str, Any], issues: _Issues) -> None:
    report["warnings"] = issues.warnings
    report["fatal_issues"] = issues.fatal
    report["can_convert"] = not issues.fatal


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_member_name(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return value


def _nonempty_string(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _epoch_timestamp(value: object) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_value(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _max_timestamp(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    if candidate is None:
        return current
    if current is None or _timestamp_value(candidate) > _timestamp_value(current):
        return candidate
    return current


def _conversation_shards(
    manifest: Mapping[str, Any], archive_names: set[str], issues: _Issues
) -> list[str]:
    logical = manifest.get("logical_files")
    if not isinstance(logical, Mapping):
        issues.fail("invalid-logical-files", "Manifest logical_files must be an object")
        return []
    descriptor = logical.get("conversations.json")
    if not isinstance(descriptor, Mapping):
        issues.fail(
            "missing-conversations-logical-file",
            "Manifest must describe logical conversations.json",
        )
        return []
    files = descriptor.get("files")
    if not isinstance(files, list) or not files:
        issues.fail(
            "invalid-conversation-shards",
            "Logical conversations.json must contain a non-empty files array",
        )
        return []
    if any(_safe_member_name(name) is None for name in files):
        issues.fail(
            "unsafe-conversation-shard-name",
            "Conversation shard names must be safe ZIP member paths",
        )
        return []
    shard_names = [str(name) for name in files]
    if len(set(shard_names)) != len(shard_names):
        issues.fail("duplicate-conversation-shard", "Conversation shards must be unique")
    if any(name not in archive_names for name in shard_names):
        issues.fail("missing-conversation-shard", "A declared conversation shard is absent")
    sharded = descriptor.get("sharded")
    if sharded is True:
        numbered = []
        for name in shard_names:
            match = SHARD_RE.fullmatch(name)
            if match is None:
                issues.fail(
                    "invalid-sharded-conversation-name",
                    "Sharded conversations must use conversations-NNN.json names",
                )
                continue
            numbered.append(int(match.group(1)))
        if numbered and sorted(numbered) != list(range(len(numbered))):
            issues.fail(
                "nonconsecutive-conversation-shards",
                "Conversation shard numbers must be consecutive from zero",
            )
    elif sharded is False:
        if shard_names != ["conversations.json"]:
            issues.fail(
                "invalid-unsharded-conversations",
                "An unsharded logical file must contain only conversations.json",
            )
    else:
        issues.fail("invalid-sharded-flag", "Logical conversations sharded must be boolean")
    shard_count = descriptor.get("shard_count")
    if not isinstance(shard_count, int) or isinstance(shard_count, bool):
        issues.fail("invalid-shard-count", "Conversation shard_count must be an integer")
    elif shard_count != len(shard_names):
        issues.fail("shard-count-mismatch", "Conversation shard_count does not match files")
    return shard_names


def _validate_graph(
    mapping: Mapping[str, Any], current_node: object, issues: _Issues
) -> list[str]:
    children = {str(node_id): [] for node_id in mapping}
    roots: list[str] = []
    for node_id, value in mapping.items():
        if not isinstance(node_id, str) or not node_id:
            issues.fail("invalid-node-id", "Mapping keys must be non-empty strings")
            continue
        if not isinstance(value, Mapping):
            issues.fail("invalid-node", "Each mapping node must be an object")
            continue
        if value.get("id") != node_id:
            issues.fail("node-id-mismatch", "Mapping key and node id must match")
        parent = value.get("parent")
        if parent is None:
            roots.append(node_id)
        elif not isinstance(parent, str) or parent not in mapping:
            issues.fail("missing-parent", "Every non-root node must reference a local parent")
        else:
            children[parent].append(node_id)
    if len(roots) != 1:
        issues.fail("invalid-root-count", "Each conversation mapping must have one root")
    if not isinstance(current_node, str) or current_node not in mapping:
        issues.fail("invalid-current-node", "Conversation current_node must exist in mapping")
    if roots:
        seen: set[str] = set()
        active: set[str] = set()
        stack: list[tuple[str, bool]] = [(roots[0], False)]
        while stack:
            node_id, exiting = stack.pop()
            if exiting:
                active.discard(node_id)
                seen.add(node_id)
                continue
            if node_id in active:
                issues.fail("mapping-cycle", "Conversation mapping must be acyclic")
                continue
            if node_id in seen:
                continue
            active.add(node_id)
            stack.append((node_id, True))
            for child in reversed(children.get(node_id, [])):
                stack.append((child, False))
        if len(seen) != len(mapping):
            issues.fail(
                "disconnected-mapping",
                "Every conversation node must be reachable from the root",
            )
    leaves = sorted(node_id for node_id, values in children.items() if not values)
    if not leaves:
        issues.fail("missing-leaf", "Conversation mapping must contain a leaf")
    return leaves


def _render_visible_content(
    content: Mapping[str, Any], issues: _Issues, report: Dict[str, Any]
) -> Optional[str]:
    parts = content.get("parts")
    if not isinstance(parts, list):
        issues.fail("invalid-content-parts", "Visible message content.parts must be an array")
        return None
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, str):
            rendered.append(part)
            continue
        if not isinstance(part, Mapping):
            issues.fail("invalid-content-part", "Visible content parts must be strings or objects")
            continue
        part_type = part.get("content_type")
        if part_type == "audio_transcription":
            text = part.get("text")
            if not isinstance(text, str):
                issues.fail(
                    "invalid-audio-transcription",
                    "Audio transcription parts require string text",
                )
                continue
            rendered.append(text)
            report["counts"]["audio_transcriptions"] += 1
            continue
        placeholder = _MEDIA_PLACEHOLDERS.get(part_type)
        if placeholder is None:
            issues.fail(
                "unsupported-content-part",
                "A visible message contains an unreviewed structured part type",
            )
            continue
        rendered.append(placeholder)
        report["counts"]["media_placeholders"] += 1
    return "\n".join(value for value in rendered if value != "")


def _render_process_content(
    content: Mapping[str, Any], content_type: str, issues: _Issues
) -> Optional[str]:
    """Render the user-visible folded reasoning block without inventing text."""

    if content_type == "reasoning_recap":
        value = content.get("content")
        if not isinstance(value, str):
            issues.fail(
                "invalid-reasoning-recap",
                "reasoning_recap content must be a string",
            )
            return None
        return value
    thoughts = content.get("thoughts")
    if not isinstance(thoughts, list):
        issues.fail("invalid-thoughts", "thoughts content must contain an array")
        return None
    rendered_items: list[str] = []
    for thought in thoughts:
        if not isinstance(thought, Mapping):
            issues.fail("invalid-thought-item", "Each thought item must be an object")
            continue
        chunks = thought.get("chunks")
        if not isinstance(chunks, list) or any(not isinstance(item, str) for item in chunks):
            issues.fail("invalid-thought-chunks", "Thought chunks must be an array of strings")
            continue
        values: list[str] = []
        for key in ("summary", "content"):
            value = thought.get(key)
            if not isinstance(value, str):
                issues.fail(
                    "invalid-thought-text",
                    "Thought summary and content must be strings",
                )
                continue
            if value.strip() and value not in values:
                values.append(value)
        for value in chunks:
            if value.strip() and value not in values:
                values.append(value)
        if values:
            rendered_items.append("\n".join(values))
    return "\n\n".join(rendered_items)


def _branch_memberships(
    conversation_id: str,
    mapping: Mapping[str, Any],
    leaves: Sequence[str],
    current_node: object,
    visible_node_ids: set[str],
    issues: _Issues,
) -> tuple[dict[str, list[tuple[str, int]]], int]:
    memberships = {node_id: [] for node_id in visible_node_ids}
    branch_count = 0
    for leaf in leaves:
        path: list[str] = []
        seen: set[str] = set()
        cursor: Optional[str] = leaf
        valid_path = True
        while cursor is not None:
            if cursor in seen:
                issues.fail("mapping-cycle", "Conversation mapping must be acyclic")
                valid_path = False
                break
            seen.add(cursor)
            path.append(cursor)
            node = mapping.get(cursor)
            if not isinstance(node, Mapping):
                valid_path = False
                break
            parent = node.get("parent")
            cursor = parent if isinstance(parent, str) else None
        if not valid_path:
            continue
        path.reverse()
        visible_path = [node_id for node_id in path if node_id in visible_node_ids]
        if not visible_path:
            continue
        kind = "0current" if leaf == current_node else "1branch"
        branch_hash = _sha256_text(f"{conversation_id}\n{leaf}")[:24]
        branch_id = f"chatgpt_official_{kind}_{branch_hash}"
        for position, node_id in enumerate(visible_path):
            memberships[node_id].append((branch_id, position))
        branch_count += 1
    for values in memberships.values():
        if not values:
            issues.fail(
                "visible-message-without-branch",
                "Every visible message must belong to at least one complete branch",
            )
        values.sort()
    return memberships, branch_count


def _inspect_conversation(
    raw: object,
    shard_name: str,
    issues: _Issues,
    report: Dict[str, Any],
) -> tuple[list[OfficialMessage], Optional[str], Optional[str]]:
    if not isinstance(raw, Mapping):
        issues.fail("invalid-conversation", "Each conversation must be an object")
        return [], None, None
    conversation_id = _nonempty_string(raw.get("id"))
    if conversation_id is None:
        issues.fail("missing-conversation-id", "Each conversation requires an id")
        return [], None, None
    if raw.get("conversation_id") != conversation_id:
        issues.fail(
            "conversation-id-mismatch", "Conversation id and conversation_id must match"
        )
    mapping = raw.get("mapping")
    if not isinstance(mapping, Mapping) or not mapping:
        issues.fail("invalid-mapping", "Each conversation requires a non-empty mapping")
        return [], conversation_id, None
    report["counts"]["mapping_nodes"] += len(mapping)
    leaves = _validate_graph(mapping, raw.get("current_node"), issues)
    retained: dict[str, tuple[str, str, str, str, Optional[str]]] = {}
    cutoff = _epoch_timestamp(raw.get("update_time"))
    if raw.get("update_time") is not None and cutoff is None:
        issues.fail(
            "invalid-conversation-update-time",
            "Conversation update_time must be epoch seconds",
        )
    for node_id, node in mapping.items():
        if not isinstance(node_id, str) or not isinstance(node, Mapping):
            continue
        message = node.get("message")
        if message is None:
            continue
        if not isinstance(message, Mapping):
            issues.fail("invalid-message", "Node message must be an object or null")
            continue
        if message.get("id") != node_id:
            issues.fail("message-id-mismatch", "Message id and mapping node id must match")
        author = message.get("author")
        role = author.get("role") if isinstance(author, Mapping) else None
        if role not in VISIBLE_ROLES:
            issues.fail("unsupported-role", "Official export contains an unreviewed role")
            continue
        content = message.get("content")
        if not isinstance(content, Mapping):
            issues.fail("invalid-content", "Message content must be an object")
            continue
        content_type = content.get("content_type")
        created_at = _epoch_timestamp(message.get("create_time"))
        if created_at is None:
            issues.fail(
                "invalid-message-time",
                "All retained messages require epoch create_time",
            )
            continue
        cutoff = _max_timestamp(cutoff, created_at)
        if content_type in PROCESS_CONTENT_TYPES:
            if role != "assistant":
                issues.fail(
                    "process-content-from-user",
                    "Folded process content is only reviewed for assistant messages",
                )
            rendered = _render_process_content(content, str(content_type), issues)
            if rendered is None:
                continue
            if not rendered.strip():
                report["counts"]["empty_process_messages_skipped"] += 1
                issues.warning(
                    "empty-process-message", "An empty folded reasoning message was skipped"
                )
                continue
            if content_type == "thoughts":
                report["counts"]["thought_messages_retained"] += 1
            else:
                report["counts"]["reasoning_recap_messages_retained"] += 1
            retained[node_id] = (
                role,
                rendered,
                created_at,
                str(content_type),
                str(content_type),
            )
            continue
        if content_type not in VISIBLE_CONTENT_TYPES:
            issues.fail(
                "unsupported-content-type",
                "Official export contains an unreviewed message content type",
            )
            continue
        rendered = _render_visible_content(content, issues, report)
        if rendered is None:
            continue
        if not rendered.strip():
            report["counts"]["empty_visible_messages_skipped"] += 1
            issues.warning("empty-visible-message", "An empty visible message was skipped")
            continue
        if role == "user":
            report["counts"]["visible_user_messages"] += 1
        else:
            report["counts"]["visible_assistant_messages"] += 1
        retained[node_id] = (
            role,
            rendered,
            created_at,
            str(content_type),
            "final_answer" if role == "assistant" else None,
        )
    memberships, branch_count = _branch_memberships(
        conversation_id,
        mapping,
        leaves,
        raw.get("current_node"),
        set(retained),
        issues,
    )
    report["counts"]["branches"] += branch_count
    result = []
    for node_id in sorted(retained):
        role, content, created_at, content_type, phase = retained[node_id]
        branches = tuple(memberships.get(node_id, []))
        report["counts"]["branch_memberships"] += len(branches)
        result.append(
            OfficialMessage(
                conversation_id=conversation_id,
                message_id=node_id,
                role=role,
                content=content,
                created_at=created_at,
                shard_name=shard_name,
                content_type=content_type,
                phase=phase,
                branch_memberships=branches,
            )
        )
    return result, conversation_id, cutoff


def inspect_chatgpt_official_export(path: str) -> OfficialInspection:
    """Validate one official export ZIP and return a redacted dry-run."""

    report = _blank_report()
    issues = _Issues()
    source = Path(path)
    try:
        stat = source.stat()
    except OSError:
        issues.fail("archive-unreadable", "Official export ZIP is not readable")
        _finish_report(report, issues)
        return OfficialInspection(report, [])
    report["archive_bytes"] = stat.st_size
    if not source.is_file():
        issues.fail("archive-not-file", "Official export path must be a file")
    if stat.st_size <= 0 or stat.st_size > MAX_ARCHIVE_BYTES:
        issues.fail("archive-size", "Official export ZIP has an unsafe size")
    try:
        report["archive_sha256"] = _sha256_file(source)
    except OSError:
        issues.fail("archive-hash-failed", "Official export ZIP could not be hashed")
        _finish_report(report, issues)
        return OfficialInspection(report, [])

    messages: list[OfficialMessage] = []
    seen_conversations: set[str] = set()
    cutoff: Optional[str] = None
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            report["manifest"]["entry_count"] = len(infos)
            names = [info.filename for info in infos]
            if len(set(names)) != len(names):
                issues.fail("duplicate-zip-member", "ZIP member names must be unique")
            if any(_safe_member_name(name) is None for name in names):
                issues.fail("unsafe-zip-member", "ZIP contains an unsafe member path")
            if any(info.flag_bits & 0x1 for info in infos):
                issues.fail("encrypted-zip-member", "Encrypted ZIP members are unsupported")
            bad_member = archive.testzip()
            report["manifest"]["zip_crc_ok"] = bad_member is None
            if bad_member is not None:
                issues.fail("zip-crc-failed", "A ZIP member failed CRC validation")
            info_by_name = {info.filename: info for info in infos}
            if MANIFEST_NAME not in info_by_name:
                issues.fail("missing-manifest", "Official export manifest is missing")
                _finish_report(report, issues)
                return OfficialInspection(report, [])
            try:
                with archive.open(MANIFEST_NAME) as source_manifest:
                    manifest = json.load(source_manifest)
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                issues.fail("invalid-manifest-json", "Official export manifest is invalid JSON")
                _finish_report(report, issues)
                return OfficialInspection(report, [])
            if not isinstance(manifest, Mapping):
                issues.fail("invalid-manifest", "Official export manifest must be an object")
                _finish_report(report, issues)
                return OfficialInspection(report, [])
            report["manifest"]["version"] = manifest.get("version")
            if manifest.get("version") != MANIFEST_VERSION:
                issues.fail("unsupported-manifest-version", "Unsupported export manifest version")
            if manifest.get("manifest_file") != MANIFEST_NAME:
                issues.fail("manifest-self-name", "Manifest self-name is invalid")
            declared_files = manifest.get("export_files")
            if not isinstance(declared_files, list):
                issues.fail("invalid-export-files", "Manifest export_files must be an array")
                declared_files = []
            declared_sizes: dict[str, int] = {}
            for item in declared_files:
                if not isinstance(item, Mapping):
                    issues.fail("invalid-export-file", "Manifest file entries must be objects")
                    continue
                name = _safe_member_name(item.get("path"))
                size = item.get("size_bytes")
                if name is None or not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    issues.fail("invalid-export-file", "Manifest file entry is invalid")
                    continue
                if name in declared_sizes:
                    issues.fail("duplicate-export-file", "Manifest file entries must be unique")
                declared_sizes[name] = size
            report["manifest"]["declared_file_count"] = len(declared_sizes)
            expected_names = set(names) - {MANIFEST_NAME}
            report["manifest"]["file_set_matches"] = set(declared_sizes) == expected_names
            if set(declared_sizes) != expected_names:
                issues.fail("manifest-file-set-mismatch", "Manifest and ZIP file sets differ")
            size_matches = all(
                name in info_by_name and info_by_name[name].file_size == size
                for name, size in declared_sizes.items()
            )
            report["manifest"]["sizes_match"] = size_matches
            if not size_matches:
                issues.fail("manifest-size-mismatch", "Manifest file sizes do not match ZIP")
            shard_names = _conversation_shards(manifest, set(names), issues)
            descriptor = (
                (manifest.get("logical_files") or {}).get("conversations.json")
                if isinstance(manifest.get("logical_files"), Mapping)
                else {}
            )
            report["logical_conversations"]["sharded"] = (
                descriptor.get("sharded") if isinstance(descriptor, Mapping) else None
            )
            report["logical_conversations"]["shard_count"] = len(shard_names)
            for shard_name in shard_names:
                info = info_by_name.get(shard_name)
                if info is None:
                    continue
                if info.file_size > MAX_CONVERSATION_SHARD_BYTES:
                    issues.fail(
                        "conversation-shard-too-large",
                        "A conversation shard exceeds the reviewed size limit",
                    )
                    continue
                try:
                    with archive.open(shard_name) as shard_file:
                        conversations = json.load(shard_file)
                except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                    issues.fail("invalid-conversation-json", "Conversation shard is invalid JSON")
                    continue
                if not isinstance(conversations, list):
                    issues.fail("invalid-conversation-shard", "Conversation shard must be an array")
                    continue
                report["counts"]["conversations"] += len(conversations)
                for conversation in conversations:
                    converted, conversation_id, conversation_cutoff = _inspect_conversation(
                        conversation, shard_name, issues, report
                    )
                    if conversation_id is not None:
                        if conversation_id in seen_conversations:
                            issues.fail(
                                "duplicate-conversation-id",
                                "Conversation ids must be unique across shards",
                            )
                        seen_conversations.add(conversation_id)
                    messages.extend(converted)
                    cutoff = _max_timestamp(cutoff, conversation_cutoff)
    except (zipfile.BadZipFile, OSError):
        issues.fail("invalid-zip", "Official export is not a readable ZIP archive")

    report["source_cutoff_at"] = cutoff
    if cutoff is None:
        issues.fail("missing-source-cutoff", "No reliable export coverage time was found")
    report["estimated_output_records"] = len(messages)
    _finish_report(report, issues)
    return OfficialInspection(report, messages)


def dry_run_chatgpt_official_export(path: str) -> Dict[str, Any]:
    return inspect_chatgpt_official_export(path).report


def _source_ref(message: OfficialMessage, archive_sha256: str) -> str:
    parameters = {
        "archiveSha256": archive_sha256,
        "shard": message.shard_name,
        "contentType": message.content_type,
    }
    if message.phase is not None:
        parameters["phase"] = message.phase
    query = urlencode(parameters)
    return (
        f"{ADAPTER_ID}://conversation/{quote(message.conversation_id, safe='')}"
        f"/message/{quote(message.message_id, safe='')}?{query}"
    )


def convert_chatgpt_official_export(path: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    inspection = inspect_chatgpt_official_export(path)
    report = inspection.report
    if not report["can_convert"]:
        raise ChatGPTOfficialAdapterError(
            "ChatGPT official export failed dry-run", report=report
        )
    archive_sha256 = str(report["archive_sha256"])
    source_cutoff_at = str(report["source_cutoff_at"])
    records: list[dict[str, Any]] = []
    for message in sorted(
        inspection.messages,
        key=lambda item: (item.conversation_id, item.created_at, item.message_id),
    ):
        identity_hash = _sha256_text(f"{message.conversation_id}\n{message.message_id}")
        records.append(
            {
                "record_id": f"chatgptv1_{identity_hash}",
                "source_kind": ADAPTER_ID,
                "source_ref": _source_ref(message, archive_sha256),
                "conversation_id": f"chatgpt-official:{message.conversation_id}",
                "branch_memberships": [
                    {"branch_id": branch_id, "position": position}
                    for branch_id, position in message.branch_memberships
                ],
                "message_id": f"chatgpt-official:{message.message_id}",
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
                "verified": True,
                "authority": "official",
                "source_cutoff_at": source_cutoff_at,
                "conflict_group_id": None,
            }
        )
    package = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "source": {
            "adapter": ADAPTER_ID,
            "archive_sha256": archive_sha256,
            "manifest_version": report["manifest"]["version"],
            "conversation_shard_count": report["logical_conversations"]["shard_count"],
            "source_cutoff_at": source_cutoff_at,
            "verification": "official-account-export",
            "folded_reasoning_policy": "retained-and-phase-labelled",
        },
        "records": records,
    }
    if len(records) != report["estimated_output_records"]:
        raise ChatGPTOfficialAdapterError(
            "conversion record count differed from dry-run", report=report
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
    """Atomically create a package and refuse to overwrite an existing file."""

    destination = Path(output_path)
    if destination.exists():
        raise FileExistsError(f"conversion output already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"conversion output parent does not exist: {destination.parent}"
        )
    package, report = convert_chatgpt_official_export(path)
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
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "dry_run": report,
    }
