"""Local-only comparison of a caretaker packet with the current MCP directory.

Reviewed target spans are partial evidence labels, never a complete answer key.
They are consumed after retrieval and must not be supplied to model callbacks.
"""
from __future__ import annotations

from . import assistant_baseline as baseline
from .evidence_selector import encoded
from . import evidence_packet

VERSION = "echo-caretaker-evaluation-v1"
IDENTITY = ("record_id", "source_kind", "conversation_id")


def _identity(row):
    return tuple(row.get(key) for key in IDENTITY)


def _brief_rows(brief):
    brief = evidence_packet.expand(brief)
    for item in brief.get("items", []):
        references = item.get("references")
        if references is None:
            yield "local_excerpt", {**item, "content": item.get("text", "")}
        else:
            for ref in references:
                yield "selected_quote", {**ref, "content": item.get("text", "")}
    for bundle in brief.get("source_order_context", []):
        for item in bundle.get("items", []):
            for ref in item.get("references", []):
                yield "source_context_quote", {**ref, "content": item.get("text", "")}


def verify_brief(brief, resolve_record):
    """Resolve each reference through a caller-bound read-only identity.

    An ID match or a verbatim substring in the wrong source is not a pass.
    Errors are sanitized; the report contains no source text or exception text.
    """
    try:
        brief = evidence_packet.expand(brief)
    except evidence_packet.PacketError:
        return {"references_checked": 0, "orphan_items": 0, "all_references_valid": False,
                "checks": [], "packet_error": "invalid_compact_evidence_packet"}
    checked = []
    for channel, row in _brief_rows(brief):
        reasons = []
        try:
            source = resolve_record(row.get("record_id"))
        except Exception:
            source = None
        if not isinstance(source, dict):
            reasons.append("source_unavailable")
        else:
            if _identity(row) != _identity(source):
                reasons.append("source_identity_mismatch")
            if row.get("role") != source.get("role"):
                reasons.append("speaker_mismatch")
            if row.get("created_at", row.get("recorded_at")) != source.get("created_at"):
                reasons.append("record_time_mismatch")
            first, last = (("quote_start", "quote_end") if channel in ("selected_quote", "source_context_quote")
                           else ("excerpt_start", "excerpt_end"))
            start, end, text = row.get(first), row.get(last), row.get("content")
            content = source.get("content")
            if (type(start) is not int or type(end) is not int or not isinstance(content, str)
                    or not isinstance(text, str) or not text or not 0 <= start < end <= len(content)
                    or content[start:end] != text):
                reasons.append("source_span_mismatch")
        checked.append({"record_id": row.get("record_id"), "channel": channel,
                        "valid": not reasons, "reasons": reasons})
    # Empty packets have no bad citations, but this is never an answer pass.
    items = [*brief.get("items", []), *(item for bundle in brief.get("source_order_context", [])
                                       for item in bundle.get("items", []))]
    orphan_items = sum("references" in item and not item["references"] for item in items)
    return {"references_checked": len(checked), "orphan_items": orphan_items,
            "all_references_valid": all(c["valid"] for c in checked) and not orphan_items,
            "checks": checked}


def measure_case(result, *, targets, directory, resolve_record):
    """Measure same-query outputs without turning target visibility into accuracy.

    ``directory`` must be an actual detail=brief call, not a projection of an
    already truncated full packet. Its IDs can be expanded even when a target
    span is absent from the preview; availability and visibility stay separate.
    """
    original = result["audit"]["query_shadow"]["original"]
    primary, adjacent = baseline._sources(original)
    selected_rows = list(_brief_rows(result["brief"]))
    provenance = verify_brief(result["brief"], resolve_record)
    valid_rows = [row for row, check in zip(selected_rows, provenance["checks"]) if check["valid"]]
    directory_rows = []
    for row in directory.get("memories", []):
        try:
            source = resolve_record(row.get("record_id"))
        except Exception:
            source = None
        if (isinstance(source, dict) and source.get("record_id") == row.get("record_id")
                and source.get("source_kind") == row.get("source_kind")
                and baseline.text_sha(source["content"]) == row.get("content_sha256")
                and type(row.get("preview_start")) is int and type(row.get("preview_end")) is int
                and 0 <= row["preview_start"] <= row["preview_end"] <= len(source["content"])
                and source["content"][row["preview_start"]:row["preview_end"]] == row.get("content_preview")):
            directory_rows.append(("directory_preview", {**source, "content": row.get("content_preview", "")}))
    available = {_identity(row) for _, row in directory_rows}
    directory_score = baseline._score(targets, directory_rows)
    raw_score = baseline._score(targets, primary + adjacent)
    selected_score = baseline._score(targets, valid_rows)
    before = {t["record_id"] for t in raw_score["targets"] if t["quote_visible"]}
    after = {t["record_id"] for t in selected_score["targets"] if t["quote_visible"]}
    directory_ids = {r["record_id"] for r in directory.get("memories", [])}
    original_ids = {r["record_id"] for _, r in primary}
    return {
        "schema_version": VERSION,
        "target_count": len(targets),
        "scoring": "partial_reviewed_spans_only" if targets else "unscored_no_reviewed_targets",
        "original_evidence": raw_score,
        "directory_evidence": directory_score,
        "directory_target_ids_expandable": [t["record_id"] for t in targets if _identity(t) in available],
        "caretaker_evidence": selected_score,
        "caretaker_selected_evidence": baseline._score(targets, [r for r in valid_rows if r[0] != "source_context_quote"]),
        "caretaker_context_evidence": baseline._score(targets, [r for r in valid_rows if r[0] == "source_context_quote"]),
        "gained_target_ids": sorted(after - before), "lost_target_ids": sorted(before - after),
        "provenance": provenance,
        "directory_ids_subset_of_same_query_original": directory_ids <= original_ids,
        "payload_utf8_bytes": {"original_audit": len(encoded(original)), "current_directory": len(encoded(directory)),
                               "caretaker_brief": len(encoded(result["brief"]))},
        "model_sufficiency": result["brief"].get("model_sufficiency"),
        "needs_more_evidence": result["brief"].get("needs_more_evidence"),
        "semantic_sufficiency": "manual_review_required",
        "unlabelled_evidence": "not_automatically_irrelevant_or_correct",
        "release_eligible": False,
    }
