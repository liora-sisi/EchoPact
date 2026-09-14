"""Query-time provenance for recall results spanning source archives.

Echo Pact keeps imported records immutable and source-labelled.  This module
adds a bounded response annotation so a caller can search several source
archives together without mistaking a mirrored message for independent
corroboration.  It never writes to SQLite and never asserts that semantically
similar passages describe the same real-world event.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import unicodedata
from typing import Any, Dict, Mapping, Optional, Sequence


SOURCE_PROVENANCE_SCHEMA_VERSION = "echo-pact-source-provenance-v1"
EXACT_MIRROR_RELATION = "exact_cross_source_message_mirror"
MAX_MIRROR_TIMESTAMP_DELTA_SECONDS = 1.0
MIN_MIRROR_CONTENT_CHARS = 32
MIN_MIRROR_DISTINCTIVE_CHARS = 12

_SOURCE_CHANNELS = {
    "room-ferry-backup-v1": "chatgpt",
    "codex-app-server-v1": "codex_local",
}


def _source_channel(source_kind: str) -> str:
    return _SOURCE_CHANNELS.get(source_kind, "other")


def _normalized_content(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _distinctive_enough(value: str) -> bool:
    if len(value) < MIN_MIRROR_CONTENT_CHARS:
        return False
    distinctive = {character.casefold() for character in value if character.isalnum()}
    return len(distinctive) >= MIN_MIRROR_DISTINCTIVE_CHARS


def _parse_instant(value: object) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _mirror_group_id(record_ids: Sequence[str]) -> str:
    seed = "\n".join(sorted(record_ids)).encode("utf-8")
    return "mirror-" + hashlib.sha256(seed).hexdigest()[:16]


def _source_summaries(memories: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    grouped: Dict[tuple[str, str, bool], Dict[str, Any]] = {}
    for memory in memories:
        source_kind = str(memory.get("source_kind") or "unknown")
        authority = str(memory.get("authority") or "unknown")
        verified = bool(memory.get("verified"))
        key = (source_kind, authority, verified)
        summary = grouped.setdefault(
            key,
            {
                "source_kind": source_kind,
                "source_channel": _source_channel(source_kind),
                "authority": authority,
                "verified": verified,
                "record_ids": set(),
                "conversation_ids": set(),
            },
        )
        record_id = str(memory.get("record_id") or "")
        conversation_id = str(memory.get("conversation_id") or "")
        if record_id:
            summary["record_ids"].add(record_id)
        if conversation_id:
            summary["conversation_ids"].add(conversation_id)

    rendered = []
    for key in sorted(grouped):
        summary = grouped[key]
        record_ids = sorted(summary["record_ids"])
        rendered.append(
            {
                "source_kind": summary["source_kind"],
                "source_channel": summary["source_channel"],
                "authority": summary["authority"],
                "verified": summary["verified"],
                "record_count": len(record_ids),
                "record_ids": record_ids,
                "conversation_ids": sorted(summary["conversation_ids"]),
            }
        )
    return rendered


def _exact_mirror_groups(
    memories: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    buckets: Dict[tuple[str, str], list[tuple[datetime, Mapping[str, Any]]]] = {}
    for memory in memories:
        source_kind = str(memory.get("source_kind") or "")
        if source_kind not in _SOURCE_CHANNELS:
            continue
        content = _normalized_content(memory.get("content"))
        role = str(memory.get("role") or "").strip().casefold()
        instant = _parse_instant(memory.get("created_at"))
        if not role or instant is None or not _distinctive_enough(content):
            continue
        buckets.setdefault((role, content), []).append((instant, memory))

    groups: list[Dict[str, Any]] = []
    for bucket_key in sorted(buckets):
        candidates = sorted(
            buckets[bucket_key],
            key=lambda item: (
                item[0],
                str(item[1].get("source_kind") or ""),
                str(item[1].get("record_id") or ""),
            ),
        )
        index = 0
        while index < len(candidates):
            cluster_start = candidates[index][0]
            cluster = [candidates[index][1]]
            index += 1
            while index < len(candidates):
                delta = (candidates[index][0] - cluster_start).total_seconds()
                if delta > MAX_MIRROR_TIMESTAMP_DELTA_SECONDS:
                    break
                cluster.append(candidates[index][1])
                index += 1

            source_kinds = sorted(
                {str(memory.get("source_kind") or "") for memory in cluster}
            )
            source_channels = sorted({_source_channel(kind) for kind in source_kinds})
            if source_channels != ["chatgpt", "codex_local"]:
                continue
            record_ids = sorted(
                {
                    str(memory.get("record_id") or "")
                    for memory in cluster
                    if memory.get("record_id")
                }
            )
            if len(record_ids) < 2:
                continue
            groups.append(
                {
                    "mirror_group_id": _mirror_group_id(record_ids),
                    "relation": EXACT_MIRROR_RELATION,
                    "status": "high_precision_candidate",
                    "record_ids": record_ids,
                    "source_kinds": source_kinds,
                    "source_channels": source_channels,
                    "independent_support_units": 1,
                    "message_identity_status": "exact_mirror_candidate",
                    "event_identity_status": "not_asserted",
                    "truth_status": "not_asserted",
                }
            )

    groups.sort(key=lambda group: (group["record_ids"], group["mirror_group_id"]))
    return groups


def build_source_provenance(
    memories: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Describe returned sources and high-precision cross-source mirrors."""

    safe_memories = [memory for memory in memories if isinstance(memory, Mapping)]
    sources = _source_summaries(safe_memories)
    mirror_groups = _exact_mirror_groups(safe_memories)
    mirrored_record_ids = {
        record_id
        for group in mirror_groups
        for record_id in group.get("record_ids", [])
    }
    source_channels = {summary["source_channel"] for summary in sources}
    returned_record_ids = sorted(
        {
            str(memory.get("record_id") or "")
            for memory in safe_memories
            if memory.get("record_id")
        }
    )
    if not returned_record_ids:
        status = "no_evidence"
    elif len(source_channels) <= 1:
        status = "single_source_channel"
    elif mirror_groups:
        status = "multiple_sources_with_exact_mirror_candidates"
    else:
        status = "multiple_sources_without_exact_mirror_candidates"

    return {
        "schema_version": SOURCE_PROVENANCE_SCHEMA_VERSION,
        "status": status,
        "scope": "returned_memories_only",
        "source_group_count": len(sources),
        "source_channels": sorted(source_channels),
        "sources": sources,
        "exact_cross_source_mirror_groups": mirror_groups,
        "unresolved_record_ids": [
            record_id
            for record_id in returned_record_ids
            if record_id not in mirrored_record_ids
        ],
        "policy": {
            "raw_records_remain_separate": True,
            "query_time_annotation_only": True,
            "exact_mirror_is_one_support_unit": True,
            "semantic_similarity_is_not_message_or_event_identity": True,
            "authority_does_not_auto_choose_truth": True,
        },
    }
