"""Independent blockers and historical replay; no model or private data calls."""
import copy
import json
from decimal import Decimal

import pytest

from backend.memory import evidence_selector as selection
from backend.memory import evidence_selector_trial as general_api
from backend.memory import object_evidence_review as review
from backend.memory import object_evidence_review_trial as api
from backend.memory.caretaker_summary import SummaryError
from backend.memory.summary_trial_budget import BudgetLedger
from scripts import evidence_selection_trial as cli
from scripts import object_correction_exam as exam


def example(index=2):
    case = exam.load_cases("all", gaps=True)[index]
    prepared = exam.prepare_case(case)
    gold = {row["id"]: row for row in case["records"]}
    draft = {"judgments": [{"candidate_id": cid, "relation": gold[row["record_id"]]["relation"],
        "quote": row["text"] if gold[row["record_id"]]["relation"] in ("answers", "uncertain") else ""}
        for cid, row in prepared["local"].items()], "sufficiency": case["sufficiency"],
        "gap": case["gap"], "gaps": list(case["gaps"])}
    return case, prepared, draft


def test_both_blockers_survive_brief_in_followup_order_without_mutating_answer():
    case, prepared, draft = example()
    before = copy.deepcopy((prepared, draft))
    brief = review.make_brief(draft, prepared)
    assert brief["gap"] == "object_identity"
    assert brief["gaps"] == ["object_identity", "conflicting_evidence"]
    assert brief["suggested_evidence_steps"] == ["identify_requested_objects", "reconcile_same_object_claims"]
    assert brief["model_sufficiency"] == "ambiguous" and brief["needs_more_evidence"]
    assert len(brief["items"]) == 3 and cli.score(case, prepared, draft, brief)["passed"]
    assert (prepared, draft) == before


@pytest.mark.parametrize("gaps,primary", [
    (None, "object_identity"), ("object_identity", "object_identity"),
    (["none"], "none"), (["object_identity", "object_identity"], "object_identity"),
    (["conflicting_evidence", "object_identity"], "conflicting_evidence"),
    (["object_identity", "conflicting_evidence"], "conflicting_evidence"),
    (["invented_reason"], "object_identity"), ([{}], "object_identity"),
    ([], "object_identity"), (["object_identity"], "none"),
])
def test_invalid_lists_or_primary_reason_fail_closed(gaps, primary):
    _, prepared, draft = example()
    draft.update(gaps=gaps, gap=primary)
    with pytest.raises(selection.SelectionError, match="invalid_multiple_gaps"):
        review.make_brief(draft, prepared)


def test_old_response_requires_explicit_replay_never_infers_a_new_reason():
    _, prepared, draft = example()
    del draft["gaps"]
    draft["gap"] = "conflicting_evidence"
    before = copy.deepcopy(draft)
    with pytest.raises(selection.SelectionError, match="schema"):
        review.make_brief(draft, prepared)
    brief = review.make_brief(draft, prepared, legacy_single_gap=True)
    assert brief["gap"] == "conflicting_evidence" and "gaps" not in brief
    assert brief["gap_reporting"] == "legacy_single_gap"
    assert "suggested_evidence_steps" not in brief and draft == before


def test_live_refinement_does_not_silently_enable_legacy_replay():
    case, _, draft = example()
    del draft["gaps"]
    fallback = {"original": "unchanged"}
    calls = []
    result = review.refine_object_shadow(exam.build_scout(case), fallback_brief=fallback,
        reviewer=lambda request: calls.append(request) or draft)
    assert result["status"] == "failed_original_brief_preserved"
    assert result["brief"] == fallback and len(calls) == 1


def test_known_conflict_can_be_secondary_but_neither_side_can_disappear():
    _, prepared, draft = example()
    for cid in ("c1", "c2"):
        prepared["local"][cid]["conflict_group_id"] = "synthetic-conflict"
    brief = review.make_brief(draft, prepared)
    assert brief["gap"] == "object_identity" and "conflicting_evidence" in brief["gaps"]
    without_conflict = copy.deepcopy(draft)
    without_conflict["gaps"] = ["object_identity"]
    with pytest.raises(selection.SelectionError, match="known_conflict_not_marked_uncertain"):
        review.make_brief(without_conflict, prepared)
    one_sided = copy.deepcopy(draft)
    one_sided["judgments"][1].update(relation="context", quote="")
    with pytest.raises(selection.SelectionError, match="known_conflict_side_omitted"):
        review.make_brief(one_sided, prepared)


def test_first_gap_agreement_does_not_excuse_losing_secondary_blocker():
    case, prepared, draft = example()
    draft["gaps"] = ["object_identity"]
    brief = review.make_brief(draft, prepared)  # Structurally valid, semantically incomplete.
    assert cli.score(case, prepared, draft, brief) == {
        "passed": False, "mismatches": [{"field": "gaps"}]}


@pytest.mark.parametrize("index", [0, 1, 3])
def test_independent_controls_do_not_require_both_gaps_everywhere(index):
    case, prepared, draft = example(index)
    brief = review.make_brief(draft, prepared)
    assert brief["gaps"] == case["gaps"] and cli.score(case, prepared, draft, brief)["passed"]
    if index == 3:
        assert brief["gap"] == "none" and not brief["suggested_evidence_steps"]
        assert brief["needs_more_evidence"]  # Local source/coverage gates still apply.


def test_excerpt_gap_cannot_be_invented_when_no_input_is_truncated():
    _, prepared, draft = example()
    draft["gaps"].append("excerpt_incomplete")
    with pytest.raises(selection.SelectionError, match="unobserved_excerpt_gap"):
        review.make_brief(draft, prepared)


@pytest.mark.parametrize("provider", ["openai", "venice"])
def test_only_object_api_requests_new_schema_and_gold_is_not_transmitted(provider):
    case, prepared, _ = example()
    payload = api.request_payload(prepared["request"], provider)
    spec = payload["text"]["format"] if provider == "openai" else payload["response_format"]["json_schema"]
    assert spec["strict"] and spec["schema"] == review.SCHEMA
    messages = payload["input"] if provider == "openai" else payload["messages"]
    body = json.loads(messages[1]["content"])
    assert not set(body) & {"gap", "gaps", "sufficiency", "records"}
    assert not any(set(row) & {"relation", "required", "allowed_relations"} for row in body["candidates"])
    plain = {k: prepared["request"][k] for k in ("question", "candidates")}
    general = general_api.request_payload(plain, provider)
    old_spec = general["text"]["format"] if provider == "openai" else general["response_format"]["json_schema"]
    assert old_spec["schema"] == selection.SCHEMA and "gaps" not in selection.SCHEMA["properties"]


def test_general_selector_does_not_accept_object_only_extension():
    _, prepared, draft = example()
    with pytest.raises(selection.SelectionError, match="schema"):
        selection.make_brief(draft, prepared)


def test_gap_exam_stops_before_key_and_cannot_amend_existing_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "read_dpapi_key", lambda *a, **k: pytest.fail("no key read"))
    path = tmp_path / "budget.json"
    data = {"schema": "echo-summary-budget-v1", "provider": "openai", "limit_usd": "5.00",
            "prior_reserved_usd": "4.999999", "reservations": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SummaryError, match="trial_budget_exhausted"):
        cli.main(["--exam", "object-gaps", "--provider", "openai", "--output-dir", str(tmp_path / "trial"),
            "--allow-synthetic-api", "--key-file", str(tmp_path / "unused.dpapi"), "--budget-ledger", str(path)])
    assert path.read_bytes() == before


def test_six_dollar_continuation_keeps_old_reservations_and_old_callers_bounded(tmp_path):
    path = tmp_path / "budget.json"
    row = {"run_id": "old", "reserved_usd": "0.821968"}
    data = {"schema": "echo-summary-budget-v1", "provider": "openai", "limit_usd": "6.00",
            "prior_reserved_usd": "4.00", "reservations": [row]}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SummaryError, match="invalid_existing"):
        with BudgetLedger(path, maximum_limit=Decimal("5.00")):
            pytest.fail("old caller may not adopt a higher limit")
    assert path.read_bytes() == before
    with BudgetLedger(path, maximum_limit=Decimal("6.00")) as ledger:
        assert ledger.total == Decimal("4.821968")
        ledger.reserve(amount=Decimal("0.50"), run_id="gaps", model="synthetic", case_id="fixed")
        assert ledger.total == Decimal("5.321968") and ledger.data["reservations"][0] == row
        with pytest.raises(SummaryError, match="exhausted"):
            ledger.reserve(amount=Decimal("0.70"), run_id="too-much", model="synthetic", case_id="blocked")
