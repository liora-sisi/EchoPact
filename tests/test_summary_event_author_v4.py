"""Dates are selected, never invented; source quotes and original drafts are immutable."""
import copy
import json

from jsonschema import Draft202012Validator
import pytest

from backend.memory import summary_event_author_v4 as author
from backend.memory import summary_reading_card as reading
from backend.memory.caretaker_summary import SummaryError
from scripts import summary_event_author_trial as trial


def sample():
    records = [{"record_id": "sample", "recorded_at": "2042-06-10T12:00:00+08:00", "source_kind": "synthetic",
        "content": "小林先说2042年6月3日去散步。随后更正为2042年6月5日去散步。6月3日晚饭的年份没写。"}]
    prepared = author.prepare_author_window(records)
    fact = {"text": "据更正，2042年6月5日去散步。", "event": {"status": "happened", "date_ref": "t2"},
        "statement_role": "correction", "supersedes": {"text": "原先说2042年6月3日去散步。", "evidence_ids": ["s1"]},
        "evidence_ids": ["s1"], "time_evidence": [], "history_time_evidence": ["t1"]}
    raw = {"status": "ok", "events": [{"event_id": "e1", "topic": "散步时间更正", "kind": "life", "facts": [fact],
        "uncertainties": []}], "uncertainties": [], "relations": [], "gaps": [], "refusal_reason": None}
    return records, prepared, raw


def test_dates_and_quotes_are_extracted_at_exact_positions_without_year_inference():
    records, prepared, raw = sample()
    assert len(prepared["date_catalog"]) == 2
    for entry in prepared["date_catalog"].values():
        assert records[0]["content"][entry["start"]:entry["end"]] == entry["quote"]
        assert records[0]["content"][entry["date_start"]:entry["date_end"]] == entry["literal"]
        assert not entry["context_truncated"]
    before = copy.deepcopy(raw)
    draft, report = author.inspect_and_project(raw, records, prepared["catalog"])
    assert raw == before and not report["errors"]
    fact = draft["events"][0]["facts"][0]
    assert fact["event_date"] == "2042-06-05" and fact["planned_date"] is None
    assert fact["text"] == before["events"][0]["facts"][0]["text"]
    assert report["semantic_passed"] is None and not report["production_accepted"]
    assert report["entries"][0]["quote"] == prepared["date_catalog"]["t2"]["quote"]


def test_unknown_ids_and_dates_attached_to_non_event_states_are_rejected():
    records, prepared, raw = sample()
    schema = Draft202012Validator(prepared["response_schema"])
    fact = raw["events"][0]["facts"][0]
    fact["event"]["date_ref"] = "2042-06-07"
    assert list(schema.iter_errors(raw))
    fact["event"] = {"status": "unknown", "date_ref": "t2"}
    assert list(schema.iter_errors(raw))
    fact["event"]["date_ref"] = None
    assert not list(schema.iter_errors(raw))


def test_old_corrected_occurrence_cannot_date_an_unrelated_dinner():
    records, prepared, raw = sample()
    dinner = {"text": "小林6月3日吃晚饭。", "event": {"status": "happened", "date_ref": "t1"},
        "statement_role": "direct", "supersedes": None, "evidence_ids": ["s1"],
        "time_evidence": [], "history_time_evidence": []}
    raw["events"].append({"event_id": "e2", "topic": "晚饭", "kind": "life", "facts": [dinner], "uncertainties": []})
    _, report = author.inspect_and_project(raw, records, prepared["catalog"])
    assert any(e["code"] == "date_index_uses_superseded_occurrence" for e in report["errors"])
    dinner["event"]["date_ref"] = None
    draft, report = author.inspect_and_project(raw, records, prepared["catalog"])
    assert not report["errors"] and draft["events"][1]["facts"][0]["event_date"] is None
    assert "6月3日" in draft["events"][1]["facts"][0]["text"]


def test_complete_date_cannot_hide_in_prose_without_a_selected_occurrence():
    records, prepared, raw = sample()
    fact = raw["events"][0]["facts"][0]
    fact["text"] += "2042年6月8日又去了一次。"
    _, report = author.inspect_and_project(raw, records, prepared["catalog"])
    assert any(e["code"] == "date_index_required" and e["date"] == "2042-06-08" for e in report["errors"])


def test_independently_sourced_dinner_may_share_the_old_calendar_value():
    records, _, raw = sample()
    records[0]["content"] += "晚饭确实在2042年6月3日，是另一次聚餐。"
    prepared = author.prepare_author_window(records)
    dinner = {"text": "晚饭在2042年6月3日。", "event": {"status": "happened", "date_ref": "t3"},
        "statement_role": "direct", "supersedes": None, "evidence_ids": ["s1"],
        "time_evidence": [], "history_time_evidence": []}
    raw["events"].append({"event_id": "e2", "topic": "另一次聚餐", "kind": "life", "facts": [dinner], "uncertainties": []})
    _, report = author.inspect_and_project(raw, records, prepared["catalog"])
    assert not report["errors"]  # Reject stale occurrences, not every event sharing that date.


def test_correction_that_keeps_the_same_date_does_not_invalidate_that_occurrence():
    records, prepared, raw = sample()
    fact = raw["events"][0]["facts"][0]
    fact["event"]["date_ref"] = "t1"
    fact["text"] = "2042年6月3日的行动对象被更正。"
    _, report = author.inspect_and_project(raw, records, prepared["catalog"])
    assert not any(e["code"] == "date_index_uses_superseded_occurrence" for e in report["errors"])
    assert report["semantic_passed"] is None  # Host does not prove the invented synthetic claim.


def test_no_complete_dates_do_not_force_a_fabricated_selection():
    records, _, raw = sample()
    records[0]["content"] = "6月3日归还雨伞，没有写年份。"
    prepared = author.prepare_author_window(records)
    assert prepared["date_catalog"] == {}
    fact = raw["events"][0]["facts"][0]
    fact.update(text="6月3日归还雨伞。", event={"status": "happened", "date_ref": None}, statement_role="direct",
                supersedes=None, history_time_evidence=[])
    projected, report = author.inspect_and_project(raw, records, prepared["catalog"])
    assert not report["errors"] and projected["events"][0]["facts"][0]["event_date"] is None


def test_long_context_and_split_date_keep_real_positions_and_explicit_clipping():
    records, _, _ = sample()
    records[0]["content"] = "甲" * 235 + "2042年6月3日归还雨伞" + "乙" * 300
    prepared = author.prepare_author_window(records)
    entry = prepared["date_catalog"]["t1"]
    assert entry["context_truncated"] and entry["date"] == "2042-06-03"
    assert len(entry["evidence_ids"]) > 1
    assert records[0]["content"][entry["start"]:entry["end"]] == entry["quote"]


def test_relation_failure_does_not_hide_completed_date_diagnostics():
    records, prepared, raw = sample()
    raw["relations"] = [{"from_event": "e1", "to_event": "e1", "kind": "updates", "evidence_ids": ["s1"]}]
    job = {"case_id": "custom", "source_window_sha256": reading.fingerprint(records), "author_recipe": author.RECIPE,
        "identity": {"source_namespace": "synthetic", "room_id": "custom", "branch_id": "fixture"}, "prompt_sha256": "a" * 64}
    case = {"case_id": "custom", "records": records, "evidence_catalog": prepared["catalog"]}
    with pytest.raises(SummaryError, match="invalid_relation") as error:
        trial.inspect_final({"finish": "complete", "model": "synthetic-model", "final_content": json.dumps(raw)}, job, case)
    assert len(error.value.date_witnesses["entries"]) == 2
    assert error.value.date_witnesses["semantic_passed"] is None


def test_v4_still_uses_the_same_raw_sources_and_local_review_targets():
    v2, v4 = trial.build_suite(), trial.build_suite(author_version="v4")
    assert v2["reviewer.json"] == v4["reviewer.json"]
    for a,b in zip(v2["jobs.json"],v4["jobs.json"]):
        before = json.loads(a["request"]["input"][1]["content"])
        after = json.loads(b["request"]["input"][1]["content"])
        assert before["records"] == after["records"]
        assert "review_targets" not in after
        assert b["request"]["max_output_tokens"] == 6000
