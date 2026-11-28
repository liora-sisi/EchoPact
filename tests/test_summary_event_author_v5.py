"""Cancellation dates, historical plans and their source context remain distinct."""
import copy
import json

from jsonschema import Draft202012Validator
import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory import summary_event_author_v5 as author
from backend.memory import summary_event_booklet as book
from backend.memory import summary_reading_card as reading
from scripts import summary_event_author_trial as trial
from scripts import prepare_summary_comprehensive_comparison as sources


def sample():
    records = [
        {"record_id": "plan", "recorded_at": "2044-02-03T12:00:00+08:00", "source_kind": "synthetic",
         "content": "小岑原计划2044年2月9日看展览。"},
        {"record_id": "cancel", "recorded_at": "2044-02-06T12:00:00+08:00", "source_kind": "synthetic",
         "content": "小岑在2044年2月6日决定取消看展览的安排，尚未另约时间。她仍期待以后和爱人一起去。"}]
    prepared = author.prepare_author_window(records)
    def fact(text, event, refs):
        return {"text": text, "event": event, "evidence_ids": refs,
            "statement_role": "direct", "supersedes": None, "time_evidence": [], "history_time_evidence": []}
    raw = {"status": "ok", "events": [{"event_id": "e1", "topic": "展览安排变化", "kind": "life", "facts": [
        fact("小岑原计划看展览。", {"status": "planned", "planned_for_ref": "t1", "plan_scope": "historical", "replaced_by": 2}, ["s1"]),
        fact("小岑已取消原来那次看展览的安排。", {"status": "cancelled", "cancelled_at_ref": "t2", "planned_for_ref": "t1"}, ["s1", "s2"]),
        fact("她仍期待以后和爱人一起去。", {"status": "not_applicable"}, ["s2"])],
        "uncertainties": [{"text": "以后何时去看展览尚未约定。", "evidence_ids": ["s2"]}]}],
        "uncertainties": [], "relations": [], "gaps": [], "refusal_reason": None}
    identity = {"source_namespace": "synthetic", "room_id": "timeline", "branch_id": "fixture"}
    return records, prepared, raw, identity


def audit_of(records, prepared, raw, identity):
    projected, dates = author.inspect_and_project(raw, records, prepared["catalog"])
    audit = book.make_audit(projected, records=records, catalog=prepared["catalog"], identity=identity,
        author={"provider": "test", "model": "test", "recipe": author.RECIPE, "prompt_sha256": "a" * 64})
    return audit, dates


def test_cancellation_time_and_scheduled_day_survive_cards_and_raw_is_immutable():
    records, prepared, raw, identity = sample()
    before = copy.deepcopy(raw)
    audit, dates = audit_of(records, prepared, raw, identity)
    assert raw == before and not dates["errors"] and not audit["review"]["structural_errors"]
    fact = audit["draft"]["events"][0]["facts"][1]
    assert fact["event_date"] is None and fact["planned_date"] == "2044-02-09"
    assert fact["time_context"]["cancelled_at"] == "2044-02-06"
    booklet = book.build_booklet(audit)
    body = booklet["cards"][0]["events"][0]["body"]
    selected = book.read_events(audit, booklet, event_ids=["e1"], current_records=records, identity=identity)
    assert selected["events"][0]["body"] == body
    assert "历史安排" in body and "后续=e1.f2" in body and "计划中" not in body
    assert "原定日=2044-02-09" in body and "取消决定日=2044-02-06" in body
    assert "仍期待以后和爱人一起去" in body and "尚未约定" in body
    assert dates["semantic_passed"] is None and not booklet["production_accepted"]
    assert {r for e in dates["entries"] for r in e["date_roles"]} == {"planned_for_ref", "cancelled_at_ref"}


def test_expanding_old_plan_brings_later_cancellation_source_and_never_strips_it_for_budget():
    records, prepared, raw, identity = sample()
    audit, _ = audit_of(records, prepared, raw, identity)
    booklet = book.build_booklet(audit)
    expanded = book.expand(audit, booklet, item_id="e1.f1", current_records=records, identity=identity)
    assert expanded["time_context"]["plan_scope"] == "historical"
    assert expanded["update_item_ids"] == ["e1.f2"] and set(expanded["update_refs"]) == {"s1", "s2"}
    assert set(expanded["evidence"]) == {"s1", "s2"}
    for source in expanded["evidence"].values():
        record = next(r for r in records if r["record_id"] == source["record_id"])
        assert record["content"][source["start"]:source["end"]] == source["quote"]
    withheld = book.expand(audit, booklet, item_id="e1.f1", current_records=records, identity=identity, max_bytes=512)
    assert withheld["status"] == "budget_exceeded" and "evidence" not in withheld


@pytest.mark.parametrize("target,code", [(1, "historical_plan_invalid_update"), (24, "historical_plan_invalid_update"),
                                       (3, "historical_plan_update_not_event")])
def test_historical_plan_needs_a_real_different_event_update(target, code):
    records, prepared, raw, identity = sample()
    raw["events"][0]["facts"][0]["event"]["replaced_by"] = target
    audit, _ = audit_of(records, prepared, raw, identity)
    assert code in {e["code"] for e in audit["review"]["structural_errors"]}
    assert not audit["production_accepted"]


def test_update_cycles_are_diagnosed_and_cannot_expand_forever():
    records, prepared, raw, identity = sample()
    raw["events"][0]["facts"][1]["event"] = {"status": "planned", "planned_for_ref": "t1",
        "plan_scope": "historical", "replaced_by": 1}
    audit, _ = audit_of(records, prepared, raw, identity)
    assert "historical_plan_update_cycle" in {e["code"] for e in audit["review"]["structural_errors"]}
    booklet = book.build_booklet(audit)
    with pytest.raises(SummaryError, match="invalid_plan_update"):
        book.expand(audit, booklet, item_id="e1.f1", current_records=records, identity=identity)


def test_current_plan_without_later_change_stays_current():
    records, prepared, raw, identity = sample()
    raw["events"][0]["facts"] = raw["events"][0]["facts"][:1]
    raw["events"][0]["facts"][0]["event"].update(plan_scope="current", replaced_by=None)
    audit, _ = audit_of(records, prepared, raw, identity)
    assert not audit["review"]["structural_errors"]
    assert "计划中" in book.build_booklet(audit)["cards"][0]["events"][0]["body"]
    # Source outside the authored plan still needs review; schema success is not semantic proof.
    assert audit["review"]["semantic_passed"] is None


def test_cancel_decision_does_not_require_or_fill_a_missing_scheduled_date():
    records, prepared, raw, identity = sample()
    cancelled = raw["events"][0]["facts"][1]
    cancelled["event"]["planned_for_ref"] = None
    audit, dates = audit_of(records, prepared, raw, identity)
    projected = audit["draft"]["events"][0]["facts"][1]
    assert projected["planned_date"] is None and projected["time_context"]["cancelled_at"] == "2044-02-06"
    assert not dates["errors"]
    cancelled["event"].update(cancelled_at_ref=None, planned_for_ref="t1")
    audit, dates = audit_of(records, prepared, raw, identity)
    projected = audit["draft"]["events"][0]["facts"][1]
    assert projected["time_context"]["cancelled_at"] is None and projected["planned_date"] == "2044-02-09"


def test_schema_does_not_accept_legacy_ambiguous_date_or_unsupported_roles():
    _, prepared, raw, _ = sample()
    schema = Draft202012Validator(prepared["response_schema"])
    current = raw["events"][0]["facts"][1]
    for when in ({"status": "cancelled", "date_ref": "t2"},
                 {"status": "unknown", "occurred_at_ref": "t2"},
                 {"status": "planned", "planned_for_ref": "t1", "plan_scope": "historical", "replaced_by": None},
                 {"status": "planned", "planned_for_ref": "t1", "plan_scope": "current", "replaced_by": 2}):
        current["event"] = when
        assert list(schema.iter_errors(raw))


def test_metadata_tampering_cannot_relabel_history_or_add_an_unsupported_cancellation_date():
    records, prepared, raw, identity = sample()
    audit, _ = audit_of(records, prepared, raw, identity)
    changed = copy.deepcopy(audit)
    changed["draft"]["events"][0]["facts"][0]["time_context"]["plan_scope"] = "current"
    with pytest.raises(SummaryError, match="audit_changed"):
        book.build_booklet(changed)
    changed = copy.deepcopy(audit["draft"])
    changed["events"][0]["facts"][1]["time_context"]["cancelled_at"] = "2044-03-01"
    checked = book.make_audit(changed, records=records, catalog=prepared["catalog"], identity=identity, author=audit["author"])
    assert "cancellation_date_without_evidence" in {e["code"] for e in checked["review"]["structural_errors"]}


def test_swapped_existing_dates_still_require_semantic_review():
    records, prepared, raw, identity = sample()
    raw["events"][0]["facts"][1]["event"].update(cancelled_at_ref="t1", planned_for_ref="t2")
    audit, dates = audit_of(records, prepared, raw, identity)
    assert not dates["errors"] and not audit["review"]["structural_errors"]
    assert audit["review"]["semantic_passed"] is None and not audit["production_accepted"]


def test_existing_frozen_suites_stay_identical_and_new_suite_keeps_sources_and_gold(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    expected = {"v2": "07c94d0fc5456256cd3b93de1d62c122cd35388ead821ee1b8ff1e5e47361d26",
        "v3": "1f03ce70e56ac0a467d19ec9a00c763cf6270b362bca2f6f6316a397850c13be",
        "v4": "08c89f8d51f48d71cadd4aba9b4d89d56d887b35c4406d10ddeafd35197784d6"}
    for version, fingerprint in expected.items():
        assert trial.build_suite(author_version=version)["manifest.json"]["jobs_sha256"] == fingerprint
    before, suite = trial.build_suite(), trial.build_suite(author_version="v5")
    assert before["reviewer.json"] == suite["reviewer.json"]
    for a, b in zip(before["jobs.json"], suite["jobs.json"]):
        assert a["source_window_sha256"] == b["source_window_sha256"]
        assert "review_targets" not in json.dumps(b["request"])
    trial.freeze(tmp_path / "frozen", suite)
    assert trial.load_frozen(tmp_path / "frozen") == suite


def test_new_trial_checks_each_fact_and_keeps_cancel_time_in_cards():
    records, prepared, raw, identity = sample()
    job = {"case_id": "sample", "source_window_sha256": reading.fingerprint(records),
        "author_recipe": author.RECIPE, "identity": identity, "prompt_sha256": "a" * 64}
    case = {"case_id": "sample", "records": records, "evidence_catalog": prepared["catalog"]}
    result = trial.inspect_final({"finish": "complete", "model": "test", "final_content": json.dumps(raw)}, job, case)
    assert result["metrics"]["exact_expansion_items_checked"] == 4
    assert not result["metrics"]["structural_errors"] and not result["metrics"]["date_witness_errors"]
    assert "取消决定日=2044-02-06" in result["booklet"]["cards"][0]["events"][0]["body"]
