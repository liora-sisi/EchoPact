"""Provider-independent, no-model caretaker contracts over one recall packet.

The shadow reads an already authorized packet. It neither retrieves additional
records nor changes the production answer. Excerpts are source text, not a
generated summary; every withheld item has a reason and a source record ID.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .event_timeline import classify_mention_types


SHADOW_SCHEMA = "echo-pact-caretaker-shadow-v1"
PLAN_SCHEMA = "echo-pact-query-plan-v1"
PASS_SCHEMA = "echo-pact-retrieval-pass-v1"
AUDIT_SCHEMA = "echo-pact-evidence-audit-v1"
BRIEF_SCHEMA = "echo-pact-evidence-brief-v1"
MAX_ITEMS = 10
MAX_PASSES = 4
MAX_EXCERPT_CHARS = 320
MAX_BRIEF_CONTENT_CHARS = 3200


class ContractError(ValueError):
    """A shadow contract is inconsistent; the original recall remains usable."""


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def validate_query_plan(plan: Mapping[str, Any], original_query: str) -> None:
    """M21 accepts only the deterministic original-query plan.

    Model rewrites require a later version with its own validation. A provider
    cannot smuggle identity, database paths or larger budgets into this schema.
    """

    if set(plan) != {
        "schema_version", "generator", "original_query", "intent", "queries",
        "calendar_scope", "budget",
    }:
        raise ContractError("unexpected plan fields")
    if plan["schema_version"] != PLAN_SCHEMA or plan["generator"] != "deterministic":
        raise ContractError("unsupported planner")
    if plan["original_query"] != original_query or not original_query.strip():
        raise ContractError("original query changed")
    if len(original_query) > 2000:
        raise ContractError("query exceeds shadow budget")
    if plan["queries"] != [{"query": original_query, "origin": "original"}]:
        raise ContractError("M21 does not execute rewritten queries")
    if plan["intent"] not in {"record_calendar_scope", "recall"}:
        raise ContractError("invalid intent")
    budget = plan["budget"]
    if not isinstance(budget, Mapping) or set(budget) != {
        "max_retrieval_passes", "max_evidence_items", "max_brief_content_chars", "model_calls",
    }:
        raise ContractError("invalid budget")
    if any(type(value) is not int for value in budget.values()):
        raise ContractError("budgets must be integers")
    if budget["max_retrieval_passes"] != MAX_PASSES or budget["model_calls"] != 0:
        raise ContractError("invalid call budget")
    size = budget["max_evidence_items"]
    if type(size) is not int or not 1 <= size <= MAX_ITEMS:
        raise ContractError("invalid item budget")
    if budget["max_brief_content_chars"] != MAX_BRIEF_CONTENT_CHARS:
        raise ContractError("invalid content budget")
    scope = plan["calendar_scope"]
    if not isinstance(scope, Mapping) or set(scope) != {
        "status", "reference_at", "reference_source", "filter_start_at",
        "filter_end_at_exclusive", "basis",
    } or scope.get("basis") != "record_time_not_event_time":
        raise ContractError("invalid calendar contract")
    if scope["status"] == "resolved_calendar_scope":
        start = _instant(scope["filter_start_at"])
        end = _instant(scope["filter_end_at_exclusive"])
        if start is None or end is None or start >= end:
            raise ContractError("invalid calendar bounds")


def build_shadow_packet(recall: Mapping[str, Any], *, query: str, limit: int) -> dict:
    scope = recall.get("temporal_scope") or {}
    plan = {
        "schema_version": PLAN_SCHEMA,
        "generator": "deterministic",
        "original_query": query,
        "intent": "record_calendar_scope" if scope.get("status") != "not_requested" and scope else "recall",
        "queries": [{"query": query, "origin": "original"}],
        "calendar_scope": {
            "status": scope.get("status", "not_requested"),
            "reference_at": scope.get("reference_at"),
            "reference_source": scope.get("reference_source"),
            "filter_start_at": scope.get("filter_start_at"),
            "filter_end_at_exclusive": scope.get("filter_end_at_exclusive"),
            "basis": "record_time_not_event_time",
        },
        "budget": {
            "max_retrieval_passes": MAX_PASSES, "max_evidence_items": limit,
            "max_brief_content_chars": MAX_BRIEF_CONTENT_CHARS, "model_calls": 0,
        },
    }
    validate_query_plan(plan, query)
    adaptive = recall.get("adaptive_recall") or {}
    raw_passes = adaptive.get("passes") or []
    if len(raw_passes) > MAX_PASSES or adaptive.get("query_passes_used", 0) > MAX_PASSES:
        raise ContractError("retrieval exceeded budget")
    passes = [{
        "schema_version": PASS_SCHEMA,
        "name": item.get("pass"), "mode": item.get("recall_mode"),
        "result_count": item.get("result_count"),
        "evidence_channel": (
            "later_retelling" if item.get("pass") == "outside_scope_retelling_trace" else "primary"
        ),
    } for item in raw_passes]

    raw_memories = recall.get("memories") or []
    if not isinstance(raw_memories, list) or len(raw_memories) > MAX_ITEMS:
        raise ContractError("primary packet exceeds shadow budget")
    start = _instant(scope.get("filter_start_at"))
    end = _instant(scope.get("filter_end_at_exclusive"))
    scoped = scope.get("status") == "resolved_calendar_scope"
    unresolved = str(scope.get("status", "")).startswith("unresolved_")
    items, excluded, audit_rows = [], [], []
    seen = set()
    remaining_chars = MAX_BRIEF_CONTENT_CHARS
    for memory in raw_memories:
        if not isinstance(memory, Mapping):
            raise ContractError("invalid evidence item")
        record_id = memory.get("record_id")
        if not isinstance(record_id, str) or not record_id or record_id in seen:
            raise ContractError("missing or duplicate source ID")
        seen.add(record_id)
        mentioned = _instant(memory.get("created_at"))
        reason = None
        if unresolved:
            reason = "unresolved_calendar_scope"
        elif scoped and (mentioned is None or not start <= mentioned < end):
            reason = "outside_record_time_scope"
        elif len(items) >= limit or remaining_chars <= 0:
            reason = "brief_budget"
        content = memory.get("content")
        if not isinstance(content, str) or not content:
            reason = reason or "missing_source_text"
        audit_rows.append({
            "record_id": record_id,
            "record_time_scope": "inside" if scoped and reason != "outside_record_time_scope" else "not_confirmed",
            "event_time": "not_inferred_from_record_time",
            "mention_types": classify_mention_types(content, memory.get("role")),
            "conflict_group_id": memory.get("conflict_group_id"),
            "decision": "excluded_from_brief" if reason else "source_excerpt",
            "reason": reason,
        })
        if reason:
            excluded.append({"record_id": record_id, "reason": reason})
            continue
        excerpt = content[:min(MAX_EXCERPT_CHARS, remaining_chars)]
        remaining_chars -= len(excerpt)
        items.append({
            "kind": "source_excerpt", "record_id": record_id,
            "source_kind": memory.get("source_kind"),
            "conversation_id": memory.get("conversation_id"),
            "role": memory.get("role"), "recorded_at": memory.get("created_at"),
            "content_phase": memory.get("content_phase"),
            "text": excerpt, "excerpt_start": 0, "excerpt_end": len(excerpt),
            "source_chars": len(content), "excerpt_truncated": len(excerpt) < len(content),
            "event_time": "not_inferred_from_record_time",
        })
    context_count = sum(len(item.get("conversation_context") or []) for item in raw_memories)
    retelling_count = len(scope.get("outside_scope_retellings") or [])
    return {
        "schema_version": SHADOW_SCHEMA, "status": "shadow_only",
        "query_plan": plan, "retrieval_passes": passes,
        "evidence_audit": {
            "schema_version": AUDIT_SCHEMA, "items": audit_rows,
            "adjacent_context_count": context_count,
            "outside_scope_retelling_count": retelling_count,
            "context_policy": "available_in_original_packet_not_primary_brief_evidence",
            "semantic_sufficiency": "not_assessed",
            "permission_basis": "already_identity_filtered_recall_packet",
        },
        "evidence_brief": {
            "schema_version": BRIEF_SCHEMA,
            "generation_status": "extractive_only_no_model_summary",
            "items": items, "excluded": excluded,
            "exhaustive": False,
            "content_chars": MAX_BRIEF_CONTENT_CHARS - remaining_chars,
            "raw_records_modified": False,
            "original_packet_available": True,
            "coverage": recall.get("coverage"),
            "limitations": [
                "excerpts_are_not_verified_facts_or_a_complete_day_summary",
                "no_excerpt_or_model_refusal_does_not_mean_no_record_exists",
                "record_time_does_not_prove_event_time",
            ],
        },
        "external_model_calls": 0, "additional_retrieval_calls": 0,
    }


def attach_caretaker_shadow(recall: dict, *, query: str, limit: int) -> dict:
    """Fail back to the untouched recall if shadow construction fails."""

    try:
        shadow = build_shadow_packet(recall, query=query, limit=limit)
    except Exception:
        # Exceptions can contain source text. Expose a stable status only.
        shadow = {"schema_version": SHADOW_SCHEMA, "status": "failed_original_preserved"}
    return {**recall, "caretaker_shadow": shadow}
