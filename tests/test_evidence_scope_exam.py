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
    return {"judgments": [
        {"candidate_id": cid, "relation": gold[row["record_id"]]["relation"],
         "quote": row["text"] if gold[row["record_id"]]["relation"] in ("answers", "uncertain") else ""}
        for cid, row in prepared["local"].items()],
        "sufficiency": case["sufficiency"], "gap": case["gap"]}


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
    draft.update(sufficiency="sufficient", gap="none")
    brief = reviewer.make_brief(draft, prepared)
    assert not cli.score(case, prepared, draft, brief)["passed"]


@pytest.mark.parametrize("exam,fixture", [
    ("scope-general", "tests/fixtures/evidence_scope_synthetic.json"),
    ("scope-objects", "tests/fixtures/object_identity_scope_synthetic.json"),
])
def test_new_scope_exams_freeze_only_fixed_synthetic_requests(tmp_path, monkeypatch, exam, fixture):
    monkeypatch.setattr(cli, "read_dpapi_key", lambda *a, **k: pytest.fail("no key read"))
    output = tmp_path / "exam"
    args = ["--exam", exam, "--provider", "openai", "--output-dir", str(output)]
    assert cli.main(args) == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic_only"] and fixture in manifest["source_sha256"]
    assert len(list(output.glob("*.request.json"))) == 2
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
