"""Offline measurement of assistant recall, using frozen, reviewed local cases.

This is an evaluation harness, not a planner or a new retrieval path. Reviewed
diagnostic clues and answer spans never reach the first natural-query call.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import re
import time

from .caretaker_shadow import build_shadow_packet

SUITE_SCHEMA = "echo-assistant-recall-cases-v1"
REPORT_SCHEMA = "echo-assistant-recall-baseline-v1"
CONTEXT_FIELDS = ("conversation_context", "event_evidence", "supporting_phase_records")


class BaselineError(ValueError):
    """Only safe, fixed error codes should cross the CLI boundary."""


def text_sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def serialized(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise BaselineError("unexpected_case_fields")


def _text(value, limit=2000):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_suite(suite):
    _fields(suite, "schema_version corpus_sha256 reference_at cases")
    if suite["schema_version"] != SUITE_SCHEMA or not _sha(suite["corpus_sha256"]):
        raise BaselineError("invalid_suite_identity")
    try:
        stamp = datetime.fromisoformat(suite["reference_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise BaselineError("frozen_reference_time_required") from None
    cases = suite["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 12:
        raise BaselineError("invalid_case_count")
    ids = set()
    for case in cases:
        _fields(case, "case_id query_origin steps targets reviewed_by")
        if (not isinstance(case["case_id"], str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", case["case_id"])
                or case["case_id"] in ids):
            raise BaselineError("invalid_case_id")
        ids.add(case["case_id"])
        if case["query_origin"] not in {"captured_original", "reconstructed_from_reported_issue", "synthetic"}:
            raise BaselineError("query_origin_required")
        if not isinstance(case["steps"], list) or not 1 <= len(case["steps"]) <= 3:
            raise BaselineError("invalid_step_count")
        for i, step in enumerate(case["steps"]):
            _fields(step, "query origin")
            if (not _text(step["query"])
                    or step["origin"] != ("natural" if i == 0 else "reviewer_diagnostic_clue")):
                raise BaselineError("invalid_query_step")
        targets = case["targets"]
        if not isinstance(targets, list) or len(targets) > 6:
            raise BaselineError("invalid_target_count")
        if targets and not _text(case["reviewed_by"], 120):
            raise BaselineError("reviewed_targets_required")
        if not targets and case["reviewed_by"] is not None:
            raise BaselineError("empty_targets_are_unscored")
        for target in targets:
            _fields(target, "record_id content_sha256 quote source_kind conversation_id")
            if (not all(_text(target[k], 1000) for k in ("record_id", "quote", "source_kind", "conversation_id"))
                    or not _sha(target["content_sha256"])):
                raise BaselineError("invalid_target")


def _sources(packet):
    primary = [("primary", row) for row in packet.get("memories", [])]
    adjacent = [(field, row) for _, memory in primary for field in CONTEXT_FIELDS
                for row in memory.get(field, [])]
    return primary, adjacent


def _score(targets, rows, *, text_field="content"):
    details = []
    for target in targets:
        same = [(channel, row) for channel, row in rows if all(
            row.get(k) == target[k] for k in ("record_id", "source_kind", "conversation_id"))]
        hits = [channel for channel, row in same if target["quote"] in row.get(text_field, "")]
        details.append({"record_id": target["record_id"], "quote_sha256": text_sha(target["quote"]),
                        "record_present": bool(same), "quote_visible": bool(hits), "channels": sorted(set(hits))})
    return {"status": ("unscored" if not targets else "all_targets_visible" if all(
        d["quote_visible"] for d in details) else "target_evidence_missing"), "targets": details}


def measure_packet(packet, *, query, targets, limit):
    """Compare the original MCP payload and its existing no-model prefix brief.

    A target ID alone is not a pass: the reviewed exact span must remain visible.
    Later retellings and derived claims are not promoted to primary evidence.
    """
    shadow = build_shadow_packet(packet, query=query, limit=limit)
    brief = shadow["evidence_brief"]
    primary, adjacent = _sources(packet)
    brief_rows = [("prefix_excerpt", row) for row in brief["items"]]
    target_ids = {t["record_id"] for t in targets}
    return {
        "returned_payload_chars": len(serialized(packet)),
        "brief_payload_chars": len(serialized(brief)),
        "primary_content_chars": sum(len(r.get("content") or "") for _, r in primary),
        "adjacent_content_chars": sum(len(r.get("content") or "") for _, r in adjacent),
        "brief_content_chars": brief["content_chars"],
        "primary_count": len(primary), "adjacent_occurrences": len(adjacent),
        "primary_records": [{k: r.get(k) for k in (
            "record_id", "source_kind", "conversation_id", "created_at", "role", "content_phase")}
            for _, r in primary],
        "unlabelled_primary_count": sum(r.get("record_id") not in target_ids for _, r in primary),
        "primary_evidence": _score(targets, primary),
        "packet_evidence": _score(targets, primary + adjacent),
        "brief_evidence": _score(targets, brief_rows, text_field="text"),
        "retrieval_passes": (packet.get("adaptive_recall") or {}).get("query_passes_used"),
        "calendar_status": (packet.get("temporal_scope") or {}).get("status"),
        "generation": "existing_prefix_excerpts_no_model",
        "semantic_sufficiency": "not_assessed_exact_spans_only",
    }


def run_suite(suite, runner, *, limit=10):
    """runner receives only query, limit, and a fixed clock, never expected answers."""
    validate_suite(suite)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise BaselineError("invalid_limit")
    report = {"schema_version": REPORT_SCHEMA, "suite_sha256": text_sha(serialized(suite)),
              "corpus_sha256": suite["corpus_sha256"], "reference_at": suite["reference_at"],
              "limit": limit, "cases": [], "model_calls": 0, "retrieval_calls": 0,
              "cost_usd": "0", "kind": "frozen_diagnostic_replay_not_autonomous_planner",
              "size_unit": "unicode_characters_of_compact_json_payload_not_tokens_or_wire_bytes"}
    for case in suite["cases"]:
        entry = {"case_id": case["case_id"], "query_origin": case["query_origin"], "steps": [],
                 "first_packet_hit_round": None, "first_brief_hit_round": None,
                 "total_returned_chars": 0, "total_brief_chars": 0,
                 "returned_chars_to_first_hit": None, "brief_chars_to_first_hit": None}
        report["cases"].append(entry)
        seen = {"packet": set(), "brief": set()}
        for number, step in enumerate(case["steps"], 1):
            started = time.monotonic()
            packet = runner({"query": step["query"], "limit": limit, "as_of": suite["reference_at"]})
            report["retrieval_calls"] += 1
            measured = measure_packet(packet, query=step["query"], targets=case["targets"], limit=limit)
            entry["steps"].append({"round": number, "origin": step["origin"],
                                   "query_sha256": text_sha(step["query"]),
                                   "elapsed_seconds": round(time.monotonic() - started, 4), **measured})
            entry["total_returned_chars"] += measured["returned_payload_chars"]
            entry["total_brief_chars"] += measured["brief_payload_chars"]
            for variant, total in (("packet", "returned"), ("brief", "brief")):
                seen[variant].update(i for i, target in enumerate(measured[f"{variant}_evidence"]["targets"])
                                     if target["quote_visible"])
                entry["steps"][-1][f"cumulative_{variant}_target_count"] = len(seen[variant])
                if (entry[f"first_{variant}_hit_round"] is None
                        and case["targets"] and len(seen[variant]) == len(case["targets"])):
                    entry[f"first_{variant}_hit_round"] = number
                    entry[f"{total}_chars_to_first_hit"] = entry[f"total_{total}_chars"]
        entry["scoring"] = "reviewed_exact_evidence_only" if case["targets"] else "unscored_no_reviewed_targets"
    return copy.deepcopy(report)
