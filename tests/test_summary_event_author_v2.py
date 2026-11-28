"""Real observed failure classes, checked on unrelated synthetic sources."""
import copy

from jsonschema import Draft202012Validator
import pytest

from backend.memory import summary_event_author_v2 as author
from backend.memory import summary_event_booklet as book
from backend.memory.caretaker_summary import SummaryError
from scripts.summary_event_booklet_trial import relation_fixture


def test_every_reference_position_binds_to_spans_not_whole_records():
    sample = relation_fixture()
    sample["records"][0]["content"] = "灯光很亮。" * 120
    prepared = author.prepare_author_window(sample["records"])
    enum = list(prepared["catalog"])
    assert len(enum) > len(sample["records"])
    found = []

    def inspect(node):
        if isinstance(node, dict):
            if "evidence_ids" in node.get("properties", {}):
                found.append(node["properties"]["evidence_ids"]["items"]["enum"])
            for value in node.values(): inspect(value)
        elif isinstance(node, list):
            for value in node: inspect(value)

    inspect(prepared["response_schema"])
    assert len(found) == 5 and all(value == enum for value in found)
    validator = Draft202012Validator(prepared["response_schema"])
    draft = copy.deepcopy(sample["draft"])
    assert not list(validator.iter_errors(draft))
    for wrong in ("row-1", "s999", "other-room-s1"):
        draft["events"][0]["facts"][0]["evidence_ids"] = [wrong]
        assert list(validator.iter_errors(draft))
    draft = copy.deepcopy(sample["draft"])
    draft["gaps"] = [{"record_ids": ["s1"], "reason": "未处理"}]
    assert list(validator.iter_errors(draft))
    draft["gaps"][0]["record_ids"] = [sample["records"][0]["record_id"]]
    assert not list(validator.iter_errors(draft))


def test_legacy_audits_remain_readable_and_unknown_author_versions_are_rejected():
    sample = relation_fixture()
    before = book.make_audit(**sample)
    book.build_booklet(before)
    assert before["author"]["recipe"] == book.RECIPE
    sample["author"]["recipe"] = author.RECIPE
    current = book.make_audit(**sample)
    assert current["draft"] == before["draft"]
    sample["author"]["recipe"] = "future-unreviewed"
    with pytest.raises(SummaryError, match="invalid_author"):
        book.make_audit(**sample)


def test_complete_date_in_text_cannot_bypass_null_date_fields_or_borrow_recorded_at():
    sample = relation_fixture()
    draft = copy.deepcopy(sample["draft"])
    draft["events"] = [draft["events"][0]]
    draft["events"][0]["facts"] = [draft["events"][0]["facts"][0]]
    draft["uncertainties"] = []
    draft["events"][0]["uncertainties"] = []
    fact = draft["events"][0]["facts"][0]
    fact.update(text="2041年5月9日去了海边。", event_date=None, planned_date=None,
                supersedes=None, evidence_ids=["s1"])
    catalog = {"s1": {"quote": "5月9日去了海边。"}, "s2": {"quote": "2041年5月9日去海边，已经回来了。"}}
    before = copy.deepcopy(draft)
    assert author.inspect_text_dates(draft, catalog)[0]["code"] == "text_date_without_current_evidence"
    assert draft == before
    fact["evidence_ids"].append("s2")
    assert not author.inspect_text_dates(draft, catalog)
    fact["supersedes"] = {"text": "原先说2041-05-08去了。", "evidence_ids": ["s1"]}
    assert author.inspect_text_dates(draft, catalog)[0]["code"] == "history_text_date_without_history_evidence"
    fact["supersedes"] = None
    draft["uncertainties"] = [{"text": "不知道2041年5月10日是否回来。", "evidence_ids": ["s1"]}]
    assert author.inspect_text_dates(draft, catalog)[0]["code"] == "uncertainty_date_without_evidence"
