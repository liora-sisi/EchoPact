import copy

import pytest

from backend.memory import object_evidence_review as core
from backend.memory import caretaker_evaluation as evaluation


def prepared(texts=None, windows=None):
    texts = texts or ["原稿：青杯的盒子是红色。", "更正，青杯用蓝色盒子，刚才写串了。", "路过的话题。"]
    rows = [{"record_id": f"s{i}", "source_kind": "synthetic", "conversation_id": "room",
             "content": text, "role": "user", "created_at": "2032-01-01T00:00:00Z"}
            for i, text in enumerate(texts, 1)]
    value = core.selection.prepare({"original": {"memories": rows}, "passes": []}, query="青杯怎么区分？")
    value["request"]["source_order_windows"] = [{"candidate_ids": w} for w in (windows or [["c1", "c2"]])]
    return value, {row["record_id"]: row for row in rows}


def draft(value, selected=("c1",), discarded="context"):
    return {"judgments": [{"candidate_id": cid, "relation": "answers" if cid in selected else discarded,
                          "quote": row["text"] if cid in selected else ""} for cid, row in value["local"].items()],
            "sufficiency": "sufficient", "gap": "none"}


def ids(items):
    return {r["record_id"] for item in items for r in item["references"]}


def context_items(result):
    return [item for bundle in result["source_order_context"] for item in bundle["items"]]


@pytest.mark.parametrize("discarded", ["context", "irrelevant"])
def test_discarded_adjacent_correction_survives_as_local_context(discarded):
    value, records = prepared()
    response = draft(value, discarded=discarded)
    before = copy.deepcopy((value, response))
    result = core.make_brief(response, value)
    assert ids(result["items"]) == {"s1"}
    assert ids(context_items(result)) == {"s2"}  # s3 has no verified order link.
    assert context_items(result)[0]["text"] == records["s2"]["content"]
    assert all(i["relation"] == "context" for i in context_items(result))
    assert result["source_order_context"][0]["for_record_ids"] == ["s1"]
    assert result["model_sufficiency"] == "sufficient" and result["needs_more_evidence"] is True
    assert result["evidence_status"] == "model_judgment_with_local_order_context_review_required"
    assert evaluation.verify_brief(result, records.get)["all_references_valid"]
    assert (value, response) == before  # The model's mistake remains auditable.


def test_old_draft_context_does_not_become_a_second_answer_after_correct_selection():
    value, records = prepared()
    result = core.make_brief(draft(value, selected=("c2",)), value)
    assert ids(result["items"]) == {"s2"} and ids(context_items(result)) == {"s1"}
    assert evaluation.verify_brief(result, records.get)["all_references_valid"]


def test_no_order_link_does_not_create_context_from_ranking_or_record_time():
    value, _ = prepared()
    value["request"]["source_order_windows"] = []
    result = core.make_brief(draft(value), value)
    assert ids(result["items"]) == {"s1"} and result["source_order_context"] == []


def test_preserved_source_order_is_explicit_not_inferred_from_display_order():
    value, _ = prepared(windows=[["c2", "c1"]])
    result = core.make_brief(draft(value), value)
    bundle = result["source_order_context"][0]
    assert bundle["source_order_windows"] == [{"record_ids": ["s2", "s1"]}]
    assert bundle["basis"] == "verified_source_order_windows_not_semantic_truth"


def test_transitive_window_preserves_middle_discarded_record():
    value, _ = prepared(windows=[["c1", "c2"], ["c2", "c3"]])
    result = core.make_brief(draft(value, selected=("c1", "c3")), value)
    assert ids(result["items"]) == {"s1", "s3"}
    assert ids(context_items(result)) == {"s2"}
    assert result["source_order_context"][0]["for_record_ids"] == ["s1", "s3"]


def test_item_budget_withholds_entire_related_group_instead_of_just_context():
    value, _ = prepared([f"青杯记录 {i}" for i in range(8)], [[f"c{i}", f"c{i + 1}"] for i in range(1, 8)])
    result = core.make_brief(draft(value), value)
    assert result["items"] == result["source_order_context"] == []
    assert result["order_context_groups_withheld"] == result["omitted_selected"] == 1
    assert result["needs_more_evidence"] is True


def test_complete_early_group_gets_space_before_unrelated_later_selections(monkeypatch):
    value, _ = prepared([f"青杯记录 {i}" for i in range(8)], [[f"c{i}", f"c{i + 1}"] for i in range(1, 6)])
    monkeypatch.setattr(core.selection, "MAX_ITEMS", 2)
    result = core.make_brief(draft(value, selected=("c1", "c3", "c7", "c8")), value)
    assert ids(result["items"]) == {"s1", "s3"}
    assert ids(context_items(result)) == {"s2", "s4", "s5", "s6"}
    assert result["omitted_selected"] == 2 and result["order_context_groups_withheld"] == 0
    assert result["needs_more_evidence"] is True


def test_whole_packet_byte_budget_never_removes_only_the_neighbour(monkeypatch):
    value, _ = prepared(["青杯用红色。", "补充内容。" * 55 + "更正：青杯用蓝色。"])
    response = draft(value)
    base = core.selection.make_brief(response, value)
    cap = len(core.selection.encoded(base)) + 500
    monkeypatch.setattr(core.selection, "MAX_BRIEF_BYTES", cap)
    result = core.make_brief(response, value)
    assert result["items"] == result["source_order_context"] == []
    assert result["order_context_groups_withheld"] == 1
    assert len(core.selection.encoded(result)) <= cap


def test_equal_display_text_keeps_source_groups_separate_when_budget_full(monkeypatch):
    value, _ = prepared(["青杯原稿。", "甲分支更正。", "青杯原稿。", "乙分支更正。"], [["c1", "c2"], ["c3", "c4"]])
    monkeypatch.setattr(core.selection, "MAX_ITEMS", 2)
    monkeypatch.setattr(core, "MAX_ORDER_CONTEXT_ITEMS", 1)
    result = core.make_brief(draft(value, selected=("c1", "c3")), value)
    assert ids(result["items"]) == {"s1"}
    assert ids(context_items(result)) == {"s2"}
    assert result["source_order_context"][0]["for_record_ids"] == ["s1"]
    assert result["order_context_groups_withheld"] == 1 and result["duplicate_display_count"] == 0


def test_seven_related_sources_fit_separate_context_slots_with_same_byte_limit():
    value, records = prepared([f"青杯记录 {i}" for i in range(7)], [[f"c{i}", f"c{i + 1}"] for i in range(1, 7)])
    result = core.make_brief(draft(value, selected=("c1", "c3")), value)
    assert ids(result["items"]) == {"s1", "s3"}
    assert ids(context_items(result)) == {"s2", "s4", "s5", "s6", "s7"}
    assert len(result["items"]) <= core.selection.MAX_ITEMS
    assert len(context_items(result)) <= core.MAX_ORDER_CONTEXT_ITEMS
    assert len(core.selection.encoded(result)) <= core.selection.MAX_BRIEF_BYTES
    assert evaluation.verify_brief(result, records.get)["all_references_valid"]


def test_existing_selected_evidence_budget_gap_remains_a_gap():
    value, _ = prepared([f"青杯记录 {i}" for i in range(7)], [["c6", "c7"]])
    result = core.make_brief(draft(value, selected=tuple(value["local"])), value)
    assert not ids(result["items"]) & {"s6", "s7"}
    assert result["partial_groups_withheld"] == 1 and result["needs_more_evidence"] is True
