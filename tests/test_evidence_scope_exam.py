"""Exam integrity checks, not evidence of a model's semantic accuracy."""
import copy
import json

import pytest

from backend.memory import evidence_selector as selector
from backend.memory import object_evidence_review as reviewer
from backend.memory.caretaker_summary import SummaryError
from scripts import evidence_selection_trial as cli
from scripts import object_correction_exam as objects


def gold_draft(case, prepared):
    gold = {row["id"]: row for row in case["records"]}
    draft = {"judgments": [
        {"candidate_id": cid, "relation": gold[row["record_id"]]["relation"],
         "quote": row["text"] if gold[row["record_id"]]["relation"] in ("answers", "uncertain") else ""}
        for cid, row in prepared["local"].items()],
        "sufficiency": case["sufficiency"], "gap": case["gap"]}
    if "source_order_windows" in prepared["request"]:
        draft["gaps"] = case.get("gaps", [] if case["gap"] == "none" else [case["gap"]])
    return draft


@pytest.mark.parametrize("case", cli.load_cases("all", scope=True), ids=lambda c: c["case_id"])
def test_general_scope_gold_is_verbatim_and_never_enters_request(case):
    original = copy.deepcopy(case)
    prepared = cli.prepare_case(case)
    assert case == original
    assert set(prepared["request"]) == {"question", "candidates"}
    for card in prepared["request"]["candidates"]:
        assert set(card) == {"candidate_id", "source_kind", "role", "text", "excerpt_incomplete"}
    draft = gold_draft(case, prepared)
    assert cli.score(case, prepared, draft, selector.make_brief(draft, prepared))["passed"]


def test_identical_sources_require_different_answers_for_different_question_scopes():
    general, batch = cli.load_cases("all", scope=True)
    one, two = cli.prepare_case(general), cli.prepare_case(batch)
    assert one["request"]["candidates"] == two["request"]["candidates"]
    assert one["request"]["question"] != two["request"]["question"]
    # Both wrong answers still obey the JSON/quote contract. The quality check
    # must catch them; validating the schema alone would miss the original bug.
    for case, prepared, other in [(general, one, batch), (batch, two, general)]:
        wrong = gold_draft(other, prepared)
        brief = selector.make_brief(wrong, prepared)
        assert not cli.score(case, prepared, wrong, brief)["passed"]


def test_removing_batch_qualifier_is_exact_text_but_fails_quality():
    case = cli.load_cases("all", scope=True)[1]
    prepared = cli.prepare_case(case)
    draft = gold_draft(case, prepared)
    answer = next(row for row in draft["judgments"] if row["relation"] == "answers")
    answer["quote"] = "30克咖啡粉，分两段注水"
    brief = selector.make_brief(draft, prepared)
    assert {"record_id": "batch", "field": "required_quote"} in cli.score(case, prepared, draft, brief)["mismatches"]


def test_object_correction_does_not_automatically_resolve_missing_identity():
    case = objects.load_cases("all", scope=True)[0]
    prepared = objects.prepare_case(case)
    draft = gold_draft(case, prepared)
    correction = next(row for row in draft["judgments"] if row["relation"] == "uncertain")
    correction["relation"] = "answers"
    draft.update(sufficiency="sufficient", gap="none", gaps=[])
    brief = reviewer.make_brief(draft, prepared)
    assert not cli.score(case, prepared, draft, brief)["passed"]


@pytest.mark.parametrize("exam,fixture,count", [
    ("scope-general", "tests/fixtures/evidence_scope_synthetic.json", 2),
    ("scope-objects", "tests/fixtures/object_identity_scope_synthetic.json", 2),
    ("identity-phrasing", "tests/fixtures/object_identity_phrasing_synthetic.json", 4),
    ("object-gaps", "tests/fixtures/object_gap_synthetic.json", 4),
])
def test_new_scope_exams_freeze_only_fixed_synthetic_requests(tmp_path, monkeypatch, exam, fixture, count):
    monkeypatch.setattr(cli, "read_dpapi_key", lambda *a, **k: pytest.fail("no key read"))
    output = tmp_path / "exam"
    args = ["--exam", exam, "--provider", "openai", "--output-dir", str(output)]
    assert cli.main(args) == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic_only"] and fixture in manifest["source_sha256"]
    assert len(list(output.glob("*.request.json"))) == count
    assert cli.main(args + ["--resume"]) == 0
    request = next(output.glob("*.request.json"))
    request.write_text("{}", encoding="utf-8")
    with pytest.raises(SummaryError, match="frozen_request_changed"):
        cli.main(args + ["--resume"])
    with pytest.raises(SystemExit):
        cli.main(args + ["--cases", "private.json"])


@pytest.mark.parametrize("exam", ["scope-general", "scope-objects", "general", "objects", "object-contrasts"])
def test_new_scope_exam_does_not_increase_ledger_or_old_exam_ceiling(tmp_path, monkeypatch, exam):
    monkeypatch.setattr(cli, "read_dpapi_key", lambda *a, **k: pytest.fail("must stop before key"))
    ledger = tmp_path / "budget.json"
    ledger.write_text(json.dumps({"schema": "echo-summary-budget-v1", "provider": "openai",
        "limit_usd": "4.00", "prior_reserved_usd": "3.99999", "reservations": []}), encoding="utf-8")
    before = ledger.read_bytes()
    output = tmp_path / "exam"
    error = "trial_budget_exhausted" if exam.startswith("scope-") else "invalid_existing_budget_ledger"
    with pytest.raises(SummaryError, match=error):
        cli.main(["--exam", exam, "--partition", "all", "--provider", "openai", "--output-dir", str(output),
                  "--allow-synthetic-api", "--key-file", str(tmp_path / "unused.dpapi"),
                  "--budget-ledger", str(ledger)])
    assert ledger.read_bytes() == before
    assert not list(output.glob("*.started.json"))


def test_four_identity_questions_keep_sources_and_order_identical():
    cases = objects.load_cases("all", phrasing=True)
    requests = [objects.prepare_case(case)["request"] for case in cases]
    assert len({request["question"] for request in requests}) == 4
    assert all(request["candidates"] == requests[0]["candidates"] for request in requests)
    assert all(request["source_order_windows"] == requests[0]["source_order_windows"] for request in requests)
    assert len(requests[0]["candidates"]) == 6


def test_discarding_identity_correction_is_not_excused_by_local_context():
    case = objects.load_cases("all", phrasing=True)[1]
    prepared = objects.prepare_case(case)
    draft = gold_draft(case, prepared)
    cid = next(cid for cid, row in prepared["local"].items() if row["record_id"] == "identity-correction")
    next(row for row in draft["judgments"] if row["candidate_id"] == cid).update(relation="context", quote="")
    brief = reviewer.make_brief(draft, prepared)
    assert {"record_id": "identity-correction", "field": "relation"} in cli.score(case, prepared, draft, brief)["mismatches"]
    assert any(ref["record_id"] == "identity-correction" for group in brief["source_order_context"]
               for row in group["items"] for ref in row["references"])


def test_optional_nonanswer_labels_never_allow_selected_evidence_to_be_dropped():
    case = objects.load_cases("all", phrasing=True)[0]
    prepared = objects.prepare_case(case)
    draft = gold_draft(case, prepared)
    optional = {r["id"] for r in case["records"] if "allowed_relations" in r}
    for judgment in draft["judgments"]:
        if prepared["local"][judgment["candidate_id"]]["record_id"] in optional:
            judgment["relation"] = "irrelevant"
    assert cli.score(case, prepared, draft, reviewer.make_brief(draft, prepared))["passed"]
    next(row for row in case["records"] if row["id"] == "identity-correction")["allowed_relations"] = ["uncertain", "context"]
    with pytest.raises(SummaryError, match="invalid_optional_nonanswer_labels"):
        cli.score(case, prepared, draft, {})


def test_identity_exam_stops_before_key_without_raising_existing_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "read_dpapi_key", lambda *a, **k: pytest.fail("no key read"))
    ledger = tmp_path / "budget.json"
    ledger.write_text(json.dumps({"schema": "echo-summary-budget-v1", "provider": "openai",
        "limit_usd": "5.00", "prior_reserved_usd": "4.99999", "reservations": []}), encoding="utf-8")
    before = ledger.read_bytes()
    with pytest.raises(SummaryError, match="trial_budget_exhausted"):
        cli.main(["--exam", "identity-phrasing", "--provider", "openai", "--output-dir", str(tmp_path / "trial"),
                  "--allow-synthetic-api", "--key-file", str(tmp_path / "unused.dpapi"), "--budget-ledger", str(ledger)])
    assert ledger.read_bytes() == before
