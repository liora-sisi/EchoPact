import copy

import pytest

from backend.mcp.recall_packets import brief_result
from backend.memory import caretaker_evaluation as core
from backend.memory.assistant_baseline import text_sha


TEXT = "并非答案的前言。" * 20 + "陶杯放在蓝盒里。"
SOURCE = {"record_id": "r1", "source_kind": "synthetic", "conversation_id": "room1",
          "role": "user", "created_at": "2032-03-01T00:00:00Z", "content": TEXT}
QUOTE = "陶杯放在蓝盒里。"
TARGET = {k: SOURCE[k] for k in core.IDENTITY} | {"quote": QUOTE, "content_sha256": text_sha(TEXT)}


def setup():
    raw = {"memories": [copy.deepcopy(SOURCE)]}
    ref = {k: SOURCE[k] for k in (*core.IDENTITY, "role", "created_at")}
    ref.update(quote_start=TEXT.index(QUOTE), quote_end=len(TEXT))
    result = {"audit": {"query_shadow": {"original": raw}},
              "brief": {"items": [{"text": QUOTE, "relation": "answers", "references": [ref]}],
                        "model_sufficiency": "sufficient", "needs_more_evidence": False}}
    return result, brief_result(raw)


def measure(result, directory, targets=None, resolver=None):
    return core.measure_case(result, targets=[TARGET] if targets is None else targets, directory=directory,
                             resolve_record=resolver or (lambda rid: SOURCE if rid == "r1" else None))


def test_directory_expandability_is_not_preview_hit_or_a_quality_loss():
    result, directory = setup()
    report = measure(result, directory)
    assert report["directory_target_ids_expandable"] == ["r1"]
    assert report["directory_evidence"]["status"] == "target_evidence_missing"
    assert report["original_evidence"]["status"] == report["caretaker_evidence"]["status"] == "all_targets_visible"
    assert report["gained_target_ids"] == report["lost_target_ids"] == []
    assert report["provenance"]["all_references_valid"]
    assert report["semantic_sufficiency"] == "manual_review_required" and not report["release_eligible"]


@pytest.mark.parametrize("field,value,reason", [
    ("source_kind", "another-source", "source_identity_mismatch"),
    ("conversation_id", "another-room", "source_identity_mismatch"),
    ("role", "assistant", "speaker_mismatch"),
    ("created_at", "2032-03-02T00:00:00Z", "record_time_mismatch"),
    ("quote_start", 0, "source_span_mismatch"),
    ("quote_end", True, "source_span_mismatch"),
])
def test_correct_words_with_wrong_source_or_offset_do_not_pass(field, value, reason):
    result, directory = setup()
    result["brief"]["items"][0]["references"][0][field] = value
    report = measure(result, directory)
    assert report["caretaker_evidence"]["status"] == "target_evidence_missing"
    assert report["lost_target_ids"] == ["r1"]
    assert reason in report["provenance"]["checks"][0]["reasons"]


def test_denied_source_is_never_accepted_and_does_not_leak_exception():
    result, directory = setup()
    def denied(_):
        raise RuntimeError("secret private text")
    report = measure(result, directory, resolver=denied)
    assert not report["provenance"]["all_references_valid"]
    assert report["directory_target_ids_expandable"] == []
    assert "secret" not in str(report)


def test_missing_target_labels_and_empty_brief_are_unscored_not_a_pass():
    result, directory = setup()
    result["brief"]["items"] = []
    report = measure(result, directory, targets=[])
    assert report["caretaker_evidence"]["status"] == "unscored"
    assert report["scoring"] == "unscored_no_reviewed_targets"
    assert report["provenance"]["references_checked"] == 0 and not report["release_eligible"]


def test_orphaned_quote_and_forged_directory_hash_fail():
    result, directory = setup()
    result["brief"]["items"][0]["references"] = []
    directory["memories"][0]["content_sha256"] = "0" * 64
    report = measure(result, directory)
    assert report["directory_target_ids_expandable"] == []
    assert report["provenance"]["orphan_items"] == 1 and not report["provenance"]["all_references_valid"]


def test_local_fallback_excerpt_has_same_provenance_checks():
    result, directory = setup()
    result["brief"] = {"items": [{**{k: SOURCE[k] for k in (*core.IDENTITY, "role")},
        "recorded_at": SOURCE["created_at"], "text": TEXT, "excerpt_start": 0, "excerpt_end": len(TEXT)}]}
    report = measure(result, directory)
    assert report["provenance"]["all_references_valid"]
    assert report["caretaker_evidence"]["status"] == "all_targets_visible"
    assert report["model_sufficiency"] is None


@pytest.mark.parametrize("field,value", [("content_preview", QUOTE), ("preview_end", True)])
def test_directory_hash_does_not_make_an_incorrect_preview_valid(field, value):
    result, directory = setup()
    directory["memories"][0][field] = value
    report = measure(result, directory)
    assert report["directory_evidence"]["status"] == "target_evidence_missing"
    assert report["directory_target_ids_expandable"] == []


def test_same_text_does_not_merge_independent_provenance_checks():
    result, directory = setup()
    result["brief"]["items"][0]["references"].append({**result["brief"]["items"][0]["references"][0], "record_id": "hidden"})
    report = measure(result, directory)
    assert report["caretaker_evidence"]["status"] == "all_targets_visible"
    assert report["provenance"]["references_checked"] == 2
    assert not report["provenance"]["all_references_valid"] and not report["release_eligible"]


def test_retained_local_context_is_visible_but_not_credited_to_model_selection():
    result, directory = setup()
    item = result["brief"]["items"].pop()
    item["relation"] = "context"
    result["brief"]["source_order_context"] = [{"for_record_ids": ["other"], "items": [item]}]
    report = measure(result, directory)
    assert report["caretaker_evidence"]["status"] == report["caretaker_context_evidence"]["status"] == "all_targets_visible"
    assert report["caretaker_selected_evidence"]["status"] == "target_evidence_missing"
    assert report["provenance"]["all_references_valid"]
    assert report["caretaker_evidence"]["targets"][0]["channels"] == ["source_context_quote"]
