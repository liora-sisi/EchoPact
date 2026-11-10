"""Synthetic tests for provider-independent, local-only caretaker contracts."""

import copy
import json

import pytest

from backend.memory import caretaker_shadow as shadow_module
from backend.memory.caretaker_shadow import (
    ContractError, MAX_BRIEF_CONTENT_CHARS, attach_caretaker_shadow,
    build_shadow_packet, validate_query_plan,
)
from backend.memory.recall_debug import run_recall_debug


def _packet():
    return {
        "query": "昨天我们说过什么？",
        "memories": [{
            "record_id": "inside", "content": "私人合成记录。" * 500,
            "created_at": "2026-11-09T03:00:00Z", "role": "user",
            "source_kind": "synthetic", "conversation_id": "synthetic-room",
            "conversation_context": [{"record_id": "adjacent", "content": "别的日期"}],
        }],
        "temporal_scope": {
            "status": "resolved_calendar_scope",
            "reference_at": "2026-11-10T02:00:00Z",
            "reference_source": "caller_as_of",
            "filter_start_at": "2026-11-08T16:00:00Z",
            "filter_end_at_exclusive": "2026-11-09T16:00:00Z",
            "outside_scope_retellings": [{"record_id": "retelling", "content": "后来"}],
        },
        "adaptive_recall": {
            "query_passes_used": 1,
            "passes": [{"pass": "initial", "recall_mode": "calendar", "result_count": 1}],
        },
        "coverage": {"coverage_gap": True},
    }


def test_shadow_uses_bounded_source_excerpts_without_mutating_or_summarizing():
    original = _packet()
    before = copy.deepcopy(original)
    result = attach_caretaker_shadow(original, query=original["query"], limit=5)
    assert original == before
    assert {k: v for k, v in result.items() if k != "caretaker_shadow"} == before
    shadow = result["caretaker_shadow"]
    brief = shadow["evidence_brief"]
    assert shadow["external_model_calls"] == shadow["additional_retrieval_calls"] == 0
    assert brief["generation_status"] == "extractive_only_no_model_summary"
    item = brief["items"][0]
    assert item["text"] == original["memories"][0]["content"][item["excerpt_start"]:item["excerpt_end"]]
    assert item["excerpt_truncated"] is True
    assert item["event_time"] == "not_inferred_from_record_time"
    assert brief["content_chars"] <= MAX_BRIEF_CONTENT_CHARS
    assert brief["exhaustive"] is False
    assert brief["coverage"]["coverage_gap"] is True
    assert shadow["evidence_audit"]["adjacent_context_count"] == 1
    assert shadow["evidence_audit"]["outside_scope_retelling_count"] == 1
    assert [item["record_id"] for item in brief["items"]] == ["inside"]


def test_audit_does_not_promote_outside_date_into_brief():
    packet = _packet()
    packet["memories"].append({"record_id": "wrong-day", "content": "旧事",
                                "created_at": "2026-10-01T00:00:00Z"})
    shadow = build_shadow_packet(packet, query=packet["query"], limit=5)
    assert shadow["evidence_brief"]["excluded"] == [
        {"record_id": "wrong-day", "reason": "outside_record_time_scope"},
    ]
    assert len(packet["memories"]) == 2


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(agent_id="different-agent"),
    lambda p: p.update(original_query="another question"),
    lambda p: p.update(queries=[{"query": "invented date", "origin": "model"}]),
    lambda p: p["budget"].update(model_calls=1),
    lambda p: p["budget"].update(model_calls=False),
    lambda p: p["budget"].update(max_retrieval_passes=4.0),
    lambda p: p["budget"].update(max_retrieval_passes=100),
    lambda p: p["calendar_scope"].update(basis="event_time"),
    lambda p: p["calendar_scope"].update(filter_end_at_exclusive="invalid"),
])
def test_untrusted_plan_cannot_expand_permissions_or_budgets(mutation):
    packet = _packet()
    plan = build_shadow_packet(packet, query=packet["query"], limit=5)["query_plan"]
    mutation(plan)
    with pytest.raises(ContractError):
        validate_query_plan(plan, packet["query"])


def test_failure_preserves_original_without_leaking_exception(monkeypatch):
    original = _packet()
    def broken(*args, **kwargs):
        raise RuntimeError("private exception body")
    monkeypatch.setattr(shadow_module, "build_shadow_packet", broken)
    result = attach_caretaker_shadow(original, query=original["query"], limit=5)
    assert result["memories"] == original["memories"]
    assert result["caretaker_shadow"]["status"] == "failed_original_preserved"
    assert "private exception body" not in json.dumps(result)


def test_shadow_flag_calls_retriever_once_and_off_keeps_original_shape(monkeypatch):
    from backend.memory import recall_debug
    calls = []
    def fake(query, **kwargs):
        calls.append(query)
        return copy.deepcopy(_packet())
    monkeypatch.setattr(recall_debug, "adaptive_recall", fake)
    off = run_recall_debug("原问题", db_path="unused", agent_id="synthetic")
    assert "caretaker_shadow" not in off
    assert len(calls) == 1
    on = run_recall_debug("原问题", db_path="unused", agent_id="synthetic", caretaker_shadow=True)
    assert len(calls) == 2
    assert {k: v for k, v in on.items() if k != "caretaker_shadow"} == off


def test_empty_or_missing_text_is_visible_not_a_fabricated_summary():
    packet = _packet()
    packet["memories"][0]["content"] = ""
    brief = build_shadow_packet(packet, query=packet["query"], limit=5)["evidence_brief"]
    assert brief["items"] == []
    assert brief["excluded"] == [{"record_id": "inside", "reason": "missing_source_text"}]
    assert brief["original_packet_available"] is True
