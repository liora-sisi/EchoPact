"""Deterministic query-time event and retelling timeline annotations.

This module does not mutate the evidence store and does not assert that two
similar messages describe the same real-world event.  It exposes a bounded
chronology of the evidence already selected by recall, with explicit clock and
scope semantics so callers can reason about an original mention, later
retellings, corrections, and quoted wording without confusing archive time
with event time.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence


EVENT_TIMELINE_SCHEMA_VERSION = "echo-pact-event-timeline-v1"
EVENT_TIMELINE_GENERATOR_VERSION = "event-timeline-query-time-v1"
MAX_ORDINARY_TIMELINE_NODES = 12
MAX_TIMELINE_EXCERPT_CHARS = 480
CHENGDU_TIMEZONE_NAME = "Asia/Shanghai"
_CHENGDU_TIMEZONE = timezone(timedelta(hours=8), CHENGDU_TIMEZONE_NAME)

_RETELLING_MARKERS = (
    "后来回忆",
    "后来复盘",
    "后来提起",
    "后来又提",
    "再说起",
    "重新说起",
    "回想起来",
)
_HISTORICAL_REFERENCE_MARKERS = (
    "还记得",
    "记不记得",
    "那次",
    "当时",
    "第一次",
)
_QUOTATION_MARKERS = (
    "原话",
    "原文",
    "逐字",
    "亲口说",
    "说过",
    "当时说",
)
_CORRECTION_OR_DENIAL_MARKERS = (
    "更正",
    "纠正",
    "说错",
    "记错",
    "不是",
    "并不是",
    "并非",
    "没有发生",
    "没发生",
    "不对",
)
_DETAIL_ADDITION_MARKERS = (
    "补充一个细节",
    "再补充",
    "还有个细节",
    "还有一个细节",
    "补一句",
    "再补一句",
)
_SUMMARY_MARKERS = ("总结一下", "概括一下", "归纳一下", "简单总结")
_PLAN_MARKERS = (
    "计划",
    "打算",
    "准备以后",
    "将来想",
    "以后想",
    "等以后",
)
_QUESTION_MARKERS = ("吗", "么", "呢", "为什么", "什么时候", "哪天", "哪次")

_AWARE_ISO_RE = re.compile(
    r"(?<!\d)(?P<value>20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2}))(?!\d)",
    re.IGNORECASE,
)
_FULL_DATE_RE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})[-/.年](?P<month>\d{1,2})[-/.月]"
    r"(?P<day>\d{1,2})(?:日|号)?(?!\d)"
)
_OCCURRENCE_BINDING_MARKERS = (
    "发生在",
    "发生于",
    "事情发生",
    "事件发生",
    "时间是",
    "时间为",
    "日期是",
    "日期为",
)
_OCCURRENCE_NEGATION_MARKERS = (
    "不代表",
    "不等于",
    "并不表示",
    "不是事情发生",
    "不是事件发生",
    "并非事情发生",
    "并非事件发生",
)


def _normalize_text(value: Any) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))
    ).strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_aware_instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_iso(value: datetime) -> str:
    return value.astimezone(_CHENGDU_TIMEZONE).isoformat()


def _explicit_event_clock(content: str) -> Dict[str, Any]:
    """Extract only source-written absolute clocks; never infer an instant."""

    def is_bound_to_occurrence(match: re.Match[str]) -> bool:
        window = re.sub(
            r"\s+",
            "",
            content[max(0, match.start() - 24) : match.end() + 24],
        )
        if any(marker in window for marker in _OCCURRENCE_NEGATION_MARKERS):
            return False
        return any(marker in window for marker in _OCCURRENCE_BINDING_MARKERS)

    instant_match = _AWARE_ISO_RE.search(content)
    if instant_match and is_bound_to_occurrence(instant_match):
        parsed = _parse_aware_instant(instant_match.group("value"))
        if parsed is not None:
            return {
                "occurred_at": _utc_iso(parsed),
                "occurred_at_local": _local_iso(parsed),
                "occurred_on": None,
                "occurred_at_status": "explicit_timezone_aware_source_time",
                "occurred_at_precision": "instant",
            }

    date_match = _FULL_DATE_RE.search(content)
    if date_match and is_bound_to_occurrence(date_match):
        try:
            calendar_date = datetime(
                int(date_match.group("year")),
                int(date_match.group("month")),
                int(date_match.group("day")),
            ).date()
        except ValueError:
            calendar_date = None
        if calendar_date is not None:
            return {
                "occurred_at": None,
                "occurred_at_local": None,
                "occurred_on": calendar_date.isoformat(),
                "occurred_at_status": "calendar_date_without_reliable_timezone",
                "occurred_at_precision": "day",
            }

    return {
        "occurred_at": None,
        "occurred_at_local": None,
        "occurred_on": None,
        "occurred_at_status": "unknown",
        "occurred_at_precision": None,
    }


def classify_mention_types(content: Any, role: Any = None) -> List[str]:
    """Return stable multi-label mention facets from explicit source wording."""

    normalized = _normalize_text(content)
    compact = re.sub(r"\s+", "", normalized)
    labels: List[str] = []
    if any(marker in compact for marker in _RETELLING_MARKERS):
        labels.append("retelling_or_recollection")
    if any(marker in compact for marker in _HISTORICAL_REFERENCE_MARKERS):
        labels.append("historical_reference")
    if (
        any(marker in compact for marker in _QUOTATION_MARKERS)
        or ("“" in normalized and "”" in normalized)
        or ('"' in normalized and normalized.count('"') >= 2)
    ):
        labels.append("quoted_or_original_wording")
    if any(marker in compact for marker in _CORRECTION_OR_DENIAL_MARKERS):
        labels.append("correction_or_denial")
    if any(marker in compact for marker in _DETAIL_ADDITION_MARKERS):
        labels.append("explicit_detail_addition")
    if any(marker in compact for marker in _SUMMARY_MARKERS):
        labels.append("summary")
    if any(marker in compact for marker in _PLAN_MARKERS):
        labels.append("future_plan")
    if (
        "?" in normalized
        or "？" in normalized
        or (str(role or "").casefold() == "user" and any(
            marker in compact for marker in _QUESTION_MARKERS
        ))
    ):
        labels.append("question_or_prompt")
    if not labels:
        labels.append("direct_mention")
    return labels


def _excerpt(content: str) -> str:
    if len(content) <= MAX_TIMELINE_EXCERPT_CHARS:
        return content
    return content[:MAX_TIMELINE_EXCERPT_CHARS] + "…"


def _timeline_node(memory: Mapping[str, Any]) -> Dict[str, Any]:
    content = str(memory.get("content") or "")
    mention_types = classify_mention_types(content, memory.get("role"))
    mentioned = _parse_aware_instant(memory.get("created_at"))
    event_clock = _explicit_event_clock(content)
    record_id = str(memory.get("record_id") or "")
    branch_ids = list(memory.get("branch_ids") or [])
    if not branch_ids and memory.get("branch_id"):
        branch_ids = [memory.get("branch_id")]
    node = {
        "node_id": "mention_" + hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:20],
        "record_id": record_id,
        "conversation_id": memory.get("conversation_id"),
        "branch_ids": branch_ids,
        "message_id": memory.get("message_id"),
        "role": memory.get("role"),
        "evidence_origin": memory.get(
            "_timeline_evidence_origin", "recall_match"
        ),
        "content_excerpt": _excerpt(content),
        "mention_types": mention_types,
        "mentioned_at": _utc_iso(mentioned) if mentioned else None,
        "mentioned_at_local": _local_iso(mentioned) if mentioned else None,
        "mentioned_at_status": (
            "source_message_time_normalized_to_utc"
            if mentioned
            else "missing_or_invalid"
        ),
        **event_clock,
        "source_kind": memory.get("source_kind"),
        "source_ref": memory.get("source_ref"),
        "verified": bool(memory.get("verified")),
        "authority": memory.get("authority"),
        "conflict_group_id": memory.get("conflict_group_id"),
        "same_event_as": None,
        "same_event_status": "not_automatically_asserted",
    }
    node["preserve_even_when_ordinary_limit_reached"] = bool(
        node["conflict_group_id"]
        or "correction_or_denial" in mention_types
    )
    return node


def _deduplicate_memories(
    memories: Sequence[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    seen = set()
    result: List[Mapping[str, Any]] = []
    for memory in memories:
        record_id = str(memory.get("record_id") or "")
        if not record_id or record_id in seen:
            continue
        seen.add(record_id)
        result.append(memory)
    return result


def _expand_recalled_evidence(
    memories: Sequence[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """Include bounded shared-event context without treating it as a merge."""

    expanded: List[Mapping[str, Any]] = []
    for memory in memories:
        primary = dict(memory)
        primary["_timeline_evidence_origin"] = "recall_match"
        expanded.append(primary)
    for memory in memories:
        for evidence in memory.get("event_evidence") or []:
            if not isinstance(evidence, Mapping):
                continue
            context = dict(evidence)
            context["_timeline_evidence_origin"] = "shared_event_context"
            expanded.append(context)
    return _deduplicate_memories(expanded)


def _select_nodes(nodes: Sequence[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], bool]:
    protected = [
        node
        for node in nodes
        if node["preserve_even_when_ordinary_limit_reached"]
    ]
    protected_ids = {node["record_id"] for node in protected}
    ordinary = [node for node in nodes if node["record_id"] not in protected_ids]
    if len(ordinary) <= MAX_ORDINARY_TIMELINE_NODES:
        selected = list(nodes)
        return selected, False

    # Keep both ends of the bounded chronology. Conflict, correction, and
    # denial nodes are added independently and therefore never disappear just
    # because the ordinary-node budget is exhausted.
    first_count = (MAX_ORDINARY_TIMELINE_NODES + 1) // 2
    last_count = MAX_ORDINARY_TIMELINE_NODES - first_count
    kept_ordinary = ordinary[:first_count]
    if last_count:
        kept_ordinary += ordinary[-last_count:]
    selected_ids = {
        node["record_id"] for node in kept_ordinary + protected
    }
    selected = [node for node in nodes if node["record_id"] in selected_ids]
    return selected, len(selected) < len(nodes)


def build_event_timeline(
    query: str,
    memories: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build a deterministic, read-only chronology from recalled evidence."""

    unique_memories = _expand_recalled_evidence(memories)
    nodes = [_timeline_node(memory) for memory in unique_memories]
    def mention_sort_key(node: Mapping[str, Any]):
        parsed = _parse_aware_instant(node.get("mentioned_at"))
        return (
            parsed is None,
            parsed or datetime.max.replace(tzinfo=timezone.utc),
            str(node.get("record_id") or ""),
        )

    nodes.sort(key=mention_sort_key)
    selected, truncated = _select_nodes(nodes)
    known_mentions = [
        parsed
        for node in nodes
        if (parsed := _parse_aware_instant(node.get("mentioned_at")))
        is not None
    ]
    archive_first = _utc_iso(min(known_mentions)) if known_mentions else None
    archive_first_local = None
    if archive_first:
        parsed_first = _parse_aware_instant(archive_first)
        archive_first_local = _local_iso(parsed_first) if parsed_first else None

    input_material = {
        "query": _normalize_text(query),
        "records": sorted(
            [
            {
                "record_id": str(memory.get("record_id") or ""),
                "created_at": memory.get("created_at"),
                "content_sha256": hashlib.sha256(
                    str(memory.get("content") or "").encode("utf-8")
                ).hexdigest(),
                "conflict_group_id": memory.get("conflict_group_id"),
            }
            for memory in unique_memories
            ],
            key=lambda item: item["record_id"],
        ),
    }
    input_hash = _sha256(input_material)
    packet: Dict[str, Any] = {
        "schema_version": EVENT_TIMELINE_SCHEMA_VERSION,
        "status": (
            "no_evidence"
            if not nodes
            else "single_mention"
            if len(nodes) == 1
            else "bounded_timeline"
        ),
        "timeline_id": "timeline_" + input_hash[:24],
        "generator": {
            "name": "Echo Pact query-time event timeline",
            "version": EVENT_TIMELINE_GENERATOR_VERSION,
            "input_sha256": input_hash,
            "storage": "rebuildable_response_annotation_only",
        },
        "clock_policy": {
            "stored_instant": "UTC",
            "display_timezone": CHENGDU_TIMEZONE_NAME,
            "mentioned_at": "source message time normalized to UTC",
            "occurred_at": (
                "only an explicit timezone-aware source instant; otherwise null"
            ),
            "occurred_on": (
                "an explicit source calendar date without inventing a timezone"
            ),
            "relative_time": (
                "not resolved without a reliable timezone-aware anchor"
            ),
        },
        "archive_first_mentioned_at": archive_first,
        "archive_first_mentioned_at_local": archive_first_local,
        "archive_first_scope": {
            "kind": "bounded_recalled_evidence",
            "exhaustive": False,
            "claim": "earliest observed in this packet, not an absolute archive first",
        },
        "mention_count": {
            "value": len(nodes),
            "semantics": "at_least",
            "returned_nodes": len(selected),
            "ordinary_node_limit": MAX_ORDINARY_TIMELINE_NODES,
            "truncated": truncated,
        },
        "linkage_policy": {
            "automatic_event_merge": False,
            "same_day_similarity_is_not_identity": True,
            "cross_conversation_auto_link": False,
            "lexical_novelty_is_not_detail_addition": True,
            "conflict_and_denial_nodes_preserved": True,
        },
        "mentions": selected,
    }
    packet["semantic_payload_sha256"] = _sha256(packet)
    return packet


def suppress_event_timeline_for_coverage_gap(
    packet: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return a non-leaking marker when a dated query is outside coverage."""

    suppressed = {
        "schema_version": packet.get(
            "schema_version", EVENT_TIMELINE_SCHEMA_VERSION
        ),
        "status": "suppressed_outside_imported_coverage",
        "mentions": [],
        "mention_count": {
            "value": 0,
            "semantics": "at_least",
            "returned_nodes": 0,
            "truncated": False,
        },
        "reason": (
            "Older lexical matches cannot establish a dated event outside the "
            "imported archive boundary."
        ),
    }
    suppressed["semantic_payload_sha256"] = _sha256(suppressed)
    return suppressed
