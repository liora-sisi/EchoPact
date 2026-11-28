"""Date witness provenance, event anchors, split dates and immutable projections."""
import copy
import json

import pytest

from backend.memory import summary_event_author_v3 as author
from backend.memory import summary_event_author_v2 as previous
from backend.memory import summary_event_booklet as book
from backend.memory import summary_reading_card as reading
from backend.memory.caretaker_summary import SummaryError
from scripts import summary_event_author_trial as trial
from scripts import prepare_summary_comprehensive_comparison as sources


def sample(content="2042年6月3日，小程已经归还雨伞。小程说2042年6月4日只是原定取书日，后来取消了。"):
    records = [{"record_id": "test-row", "recorded_at": "2042-06-10T12:00:00+08:00",
                "source_kind": "synthetic", "content": content}]
    prepared = author.prepare_author_window(records)
    refs = list(prepared["catalog"])
    fact = {"text": "小程已经归还雨伞。", "event_status": "happened", "event_date": "2042-06-03",
        "planned_date": None, "statement_role": "direct", "supersedes": None, "evidence_ids": refs,
        "time_evidence": [{"date_text": "2042年6月3日", "event_phrase": "归还雨伞",
                           "quote": "2042年6月3日，小程已经归还雨伞。", "evidence_ids": refs}],
        "history_time_evidence": []}
    draft = {"status": "ok", "events": [{"event_id": "e1", "topic": "雨伞归还", "kind": "life",
        "facts": [fact], "uncertainties": []}], "uncertainties": [], "relations": [], "gaps": [], "refusal_reason": None}
    return records, prepared, draft


def inspect(records, prepared, draft):
    return author.inspect_and_project(draft, records, prepared["catalog"])


def test_exact_witnesses_are_separate_from_unchanged_reading_facts():
    records, prepared, raw = sample()
    before = copy.deepcopy(raw)
    projected, report = inspect(records, prepared, raw)
    assert not report["errors"] and raw == before
    expected = copy.deepcopy(raw)
    for field in ("time_evidence", "history_time_evidence"):
        del expected["events"][0]["facts"][0][field]
    assert projected == expected
    witness = report["entries"][0]
    assert records[0]["content"][witness["start"]:witness["end"]] == witness["quote"]
    assert report["raw_draft_sha256"] == reading.fingerprint(raw)
    assert report["projected_draft_sha256"] == reading.fingerprint(projected)
    assert report["semantic_passed"] is None and not report["production_accepted"]


@pytest.mark.parametrize("problem,code", [
    ("changed_quote", "date_witness_quote_not_exact"),
    ("wrong_event", "date_witness_event_phrase_not_shared"),
    ("two_dates", "date_witness_ambiguous_or_invalid_date"),
    ("missing", "date_witness_required"),
    ("no_claim", "date_witness_unclaimed_date"),
    ("wrong_precision", "date_witness_ambiguous_or_invalid_date"),
    ("history_borrowed", "date_witness_missing_fact_reference"),
])
def test_observed_date_errors_are_diagnostics_not_silent_repairs(problem, code):
    records, prepared, raw = sample()
    fact = raw["events"][0]["facts"][0]
    w = fact["time_evidence"][0]
    if problem == "changed_quote": w["quote"] = w["quote"].replace("归还", "买了")
    if problem == "wrong_event": w["event_phrase"] = "原定取书日"
    if problem == "two_dates": w["quote"] = records[0]["content"]
    if problem == "missing": fact["time_evidence"] = []
    if problem == "no_claim": fact["event_date"] = None
    if problem == "wrong_precision": w["date_text"] = "6月3日不是完整年月日"
    if problem == "history_borrowed":
        fact["history_time_evidence"] = copy.deepcopy(fact["time_evidence"])
    before = copy.deepcopy(raw)
    _, report = inspect(records, prepared, raw)
    assert code in {e["code"] for e in report["errors"]}
    assert raw == before and not report["production_accepted"]


def test_partial_dates_stay_in_text_without_filling_in_the_message_year():
    records, prepared, raw = sample("6月3日，小程已经归还雨伞，具体年份没写。")
    fact = raw["events"][0]["facts"][0]
    fact.update(text="6月3日，小程归还雨伞。", event_date=None, time_evidence=[])
    projected, report = inspect(records, prepared, raw)
    assert not report["errors"]
    assert projected["events"][0]["facts"][0]["event_date"] is None
    fact["text"] = "2042年6月3日，小程归还雨伞。"
    assert any(e["code"] == "date_witness_required" for e in inspect(records, prepared, raw)[1]["errors"])


def test_date_straddling_source_slices_is_supported_only_for_the_new_recipe():
    records, prepared, raw = sample("甲" * 235 + "2042年6月3日，小程已经归还雨伞。")
    assert len(prepared["catalog"]) == 2
    projected, report = inspect(records, prepared, raw)
    assert not report["errors"]
    identity = {"source_namespace": "synthetic", "room_id": "test", "branch_id": "test"}
    provenance = {"provider": "test", "model": "test", "recipe": author.RECIPE, "prompt_sha256": "a" * 64}
    audit = book.make_audit(projected, records=records, catalog=prepared["catalog"], identity=identity, author=provenance)
    assert not audit["review"]["structural_errors"]
    provenance["recipe"] = previous.RECIPE
    old = book.make_audit(projected, records=records, catalog=prepared["catalog"], identity=identity, author=provenance)
    assert any(e["code"] == "date_without_current_evidence" for e in old["review"]["structural_errors"])
    assert book.build_booklet(old)["layout_complete"]  # Legacy audit still reproduces its original checks.


def test_nonadjacent_or_cross_record_sources_cannot_be_joined_into_an_exact_quote():
    records, _, raw = sample("甲" * 235 + "2042年6月3日，小程已经归还雨伞。" + "乙" * 450)
    prepared = author.prepare_author_window(records)
    fact = raw["events"][0]["facts"][0]
    fact["time_evidence"][0]["evidence_ids"] = ["s1", "s3"]
    fact["evidence_ids"] = ["s1", "s3"]
    assert any(e["code"] == "date_witness_noncontiguous_or_cross_record" for e in inspect(records, prepared, raw)[1]["errors"])
    records.append({**records[0], "record_id": "other-record", "content": "别的记录，没有日期。"})
    prepared = author.prepare_author_window(records)
    other = list(prepared["catalog"])[-1]
    fact["time_evidence"][0]["evidence_ids"] = ["s1", other]
    fact["evidence_ids"] = ["s1", other]
    assert any(e["code"] == "date_witness_noncontiguous_or_cross_record" for e in inspect(records, prepared, raw)[1]["errors"])


def test_source_catalog_and_generated_shape_cannot_be_changed():
    records, prepared, raw = sample()
    prepared["catalog"]["s1"]["quote"] += "变造"
    with pytest.raises(SummaryError, match="catalog_changed"):
        inspect(records, prepared, raw)
    prepared = author.prepare_author_window(records)
    raw["events"][0]["facts"][0]["time_evidence"][0]["evidence_ids"] = ["test-row"]
    with pytest.raises(SummaryError, match="invalid_shape"):
        inspect(records, prepared, raw)


def test_same_sources_and_gold_stay_unchanged_across_versions_and_freezes(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    old, new = trial.build_suite(), trial.build_suite(author_version="v3")
    assert old["reviewer.json"] == new["reviewer.json"]
    for a,b in zip(old["jobs.json"],new["jobs.json"]):
        assert a["source_window_sha256"] == b["source_window_sha256"]
        assert a["request"]["input"][1] == b["request"]["input"][1]
        assert b["request"]["max_output_tokens"] == 8000
        assert "review_targets" not in json.dumps(b["request"])
    for name,suite in (("old",old),("new",new)):
        trial.freeze(tmp_path/name,suite)
        assert trial.load_frozen(tmp_path/name)==suite
    with pytest.raises(SummaryError, match="unknown_version"):
        trial.build_suite(author_version="unknown")
