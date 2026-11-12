"""Bounded, deterministic evidence previews; never generated summaries.

The full recall contract remains the audit view. Brief is an explicit allowlist,
so new debug/timeline fields cannot silently grow the assistant's context again.
Offsets are Python/Unicode code points, hashes cover the original UTF-8 text.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from backend.memory.records_v1 import _content_phase
from backend.memory.source_provenance import source_channel_for_kind


BRIEF_PREVIEW_CHARS = 120
MAX_BRIEF_JSON_BYTES = 12_000
MAX_BRIEF_MCP_RESULT_BYTES = 28_000
MAX_RECORD_JSON_BYTES = 16_000
MAX_RECORD_ID_CHARS = 256


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def json_bytes(value: Any) -> int:
    return len(json_text(value).encode("utf-8"))


def tool_result(payload: Mapping[str, Any], *, is_error: bool = False) -> dict:
    structured = dict(payload)
    return {
        "content": [{"type": "text", "text": json_text(structured)}],
        "structuredContent": structured,
        "isError": is_error,
    }


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _fields(source: Mapping, names: tuple[str, ...], *, budget: int = 1_200) -> dict:
    """Never silently shorten a provenance value into a different identifier."""
    result: dict = {}
    omitted = []
    for name in names:
        if name not in source:
            continue
        value = source[name]
        if not isinstance(value, (str, int, float, bool, type(None))):
            omitted.append(name)
            continue
        try:
            fits = json_bytes(value) <= 512 and json_bytes({**result, name: value}) <= budget
        except (ValueError, UnicodeError):
            fits = False
        if fits:
            result[name] = value
        else:
            omitted.append(name)
    if omitted:
        result["fields_omitted"] = omitted
    return result


RECORD_FIELDS = (
    "record_id", "source_kind", "role", "created_at", "verified", "authority",
    "source_cutoff_at", "conflict_group_id", "conversation_id", "message_id",
    "branch_id", "source_ref",
)
BRIEF_RECORD_FIELDS = (
    "record_id", "source_kind", "role", "created_at", "verified", "authority",
    "source_cutoff_at", "conflict_group_id", "confidence",
)


def record_metadata(record: Mapping) -> dict:
    result = _fields(record, RECORD_FIELDS, budget=3_000)
    if "verified" in result:
        result["verified"] = bool(result["verified"])
    result["source_channel"] = source_channel_for_kind(record.get("source_kind"))
    result["content_phase"] = _content_phase(record.get("source_kind"), record.get("source_ref"))
    return result


def _brief_memory(record: Mapping) -> dict | None:
    record_id = record.get("record_id")
    # An unusable ID must not become a plausible-looking, unexpandable preview.
    if not isinstance(record_id, str) or not 1 <= len(record_id) <= MAX_RECORD_ID_CHARS:
        return None
    result = _fields(record, BRIEF_RECORD_FIELDS, budget=1_800)
    if "verified" in result:
        result["verified"] = bool(result["verified"])
    result["source_channel"] = source_channel_for_kind(record.get("source_kind"))
    result["content_phase"] = _content_phase(record.get("source_kind"), record.get("source_ref"))
    if result.get("record_id") != record_id:
        return None
    content = record.get("content")
    if not isinstance(content, str):
        return None
    result.update({
        "content_chars": len(content),
        "content_sha256": content_hash(content),
        "content_preview": content[:BRIEF_PREVIEW_CHARS],
        "preview_start": 0,
        "preview_end": min(len(content), BRIEF_PREVIEW_CHARS),
        "content_truncated": len(content) > BRIEF_PREVIEW_CHARS,
    })
    for field in ("claims", "conversation_context", "event_evidence", "supporting_phase_records"):
        count = len(record.get(field) or [])
        if count:
            result[field + "_count"] = count
    return result


SECTION_FIELDS = {
    "coverage": (
        "coverage_gap", "coverage_status", "verified_knowledge_cutoff_at",
        "latest_imported_record_at", "requested_as_of",
        "contains_post_cutoff_unverified_recent_patch", "_visible_record_count",
    ),
    "temporal_scope": (
        "status", "used_for_record_filtering", "filter_start_at", "filter_end_at_exclusive",
        "resolved_on", "resolved_start_on", "resolved_end_on", "precision",
        "reference_at", "reference_source", "display_timezone",
    ),
    "temporal_coverage": (
        "coverage_gap", "status", "requested_date", "latest_imported_record_at", "date_source",
    ),
    "query_clarification": ("status", "reason", "suggested_action"),
    "record_time_filter": ("applied", "start_at", "end_at_exclusive"),
    "event_collection": ("status", "event_count_lower_bound", "exhaustive"),
    "named_collection": ("status", "item_count_lower_bound", "exhaustive"),
}


def _fits_brief(packet: dict) -> bool:
    return (json_bytes(packet) <= MAX_BRIEF_JSON_BYTES
            and json_bytes(tool_result(packet)) <= MAX_BRIEF_MCP_RESULT_BYTES)


def brief_result(raw: Mapping[str, Any]) -> dict:
    """Project the RAW recall before any text truncation, then cap the whole packet."""
    records = raw.get("memories") or []
    packet = {
        "schema_version": "echo-pact-mcp-brief-v1",
        "detail": "brief",
        "recall_mode": _fields(raw, ("recall_mode",)).get("recall_mode"),
        "evidence_notice": (
            "Previews are candidates, not summaries or complete evidence. Expand selected IDs "
            "with expected_content_sha256 before quoting or concluding. created_at is message "
            "time, not event time. Counts are not exhaustive. Mirrors are one support unit. "
            "confidence is a heuristic, not probability. Full recall is the audit view for "
            "omitted context, conflicts and timelines."
        ),
        "limits": {"json_bytes": MAX_BRIEF_JSON_BYTES, "mcp_result_bytes": MAX_BRIEF_MCP_RESULT_BYTES},
        "memories": [],
        "omitted": {"memory_count": 0, "sections": []},
    }
    for name, fields in SECTION_FIELDS.items():
        value = raw.get(name)
        if isinstance(value, Mapping):
            packet[name] = _fields(value, fields, budget=900)

    temporal = raw.get("temporal_scope") or {}
    retellings = temporal.get("outside_scope_retellings") or []
    if retellings:
        packet["temporal_scope"]["outside_scope_retellings_omitted"] = len(retellings)
    provenance = raw.get("source_provenance") or {}
    selection = provenance.get("selection") or {}
    packet["source_provenance"] = {
        **_fields(provenance, ("status", "source_group_count")),
        "mirror_group_count": len(provenance.get("exact_cross_source_mirror_groups") or []),
        "compacted_mirror_record_count": len(selection.get("compacted_mirror_record_ids") or []),
    }
    timeline = raw.get("event_timeline") or {}
    mentions = timeline.get("mentions") or []
    packet["event_timeline"] = {
        **_fields(timeline, ("status",)),
        "mentions_omitted": len(mentions),
        "conflict_or_correction_mentions_omitted": sum(
            bool(node.get("preserve_even_when_ordinary_limit_reached")) for node in mentions
        ),
    }
    # Report omitted sections without copying any of their possibly large bodies.
    packet["omitted"]["sections"] = [
        name for name in (
            "source_provenance", "event_timeline", "adaptive_recall", "recall_debug",
            "event_collection", "named_collection", "speech_query_rescue", "event_recall",
        ) if name in raw
    ]
    for record in records[:10]:
        item = _brief_memory(record)
        if item is not None:
            packet["memories"].append(item)
    packet["omitted"]["memory_count"] = len(records) - len(packet["memories"])

    # Prefer keeping all IDs with smaller previews over losing support units.
    for preview_limit in (60, 0):
        if _fits_brief(packet):
            break
        for item in packet["memories"]:
            item["content_preview"] = item["content_preview"][:preview_limit]
            item["preview_end"] = len(item["content_preview"])
            item["content_truncated"] = item["preview_end"] < item["content_chars"]
    while not _fits_brief(packet) and packet["memories"]:
        packet["memories"].pop()
        packet["omitted"]["memory_count"] += 1
    # Each allowlisted section is bounded; fail closed if a future edit breaks
    # that invariant rather than returning a surprise unbounded packet.
    if not _fits_brief(packet):
        raise ValueError("brief metadata exceeds its output budget")
    packet["metadata_incomplete"] = any(
        isinstance(value, dict) and bool(value.get("fields_omitted"))
        for value in packet.values()
    ) or any(item.get("fields_omitted") for item in packet["memories"])
    # Reserve for the final flag in the actual bound as well.
    while not _fits_brief(packet) and packet["memories"]:
        packet["memories"].pop()
        packet["omitted"]["memory_count"] += 1
    if not _fits_brief(packet):
        raise ValueError("brief metadata exceeds its output budget")
    return packet


def record_page(record: Mapping, *, offset: int, length: int) -> dict:
    content = record["content"]
    if offset > len(content):
        raise ValueError("offset exceeds record length")
    packet = {
        "schema_version": "echo-pact-mcp-record-page-v1",
        "record": record_metadata(record),
        "content_sha256": content_hash(content),
        "content_chars": len(content),
        "offset_unit": "unicode_code_points",
        "offset": offset,
        "content": content[offset:offset + length],
        "end_offset": min(offset + length, len(content)),
        "next_offset": None,
        "content_truncated": False,
        "evidence_notice": "Original text only; created_at is message time, not proof of event time.",
    }
    while True:
        end = offset + len(packet["content"])
        packet["end_offset"] = end
        packet["next_offset"] = end if end < len(content) else None
        packet["content_truncated"] = offset > 0 or end < len(content)
        if json_bytes(packet) <= MAX_RECORD_JSON_BYTES:
            return packet
        if not packet["content"]:
            raise ValueError("record metadata exceeds its output budget")
        packet["content"] = packet["content"][:len(packet["content"]) // 2]
