"""Second-round source freezing, fair comparison, provenance and bounded calls."""
import copy
from decimal import Decimal
import json

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory.summary_trial_budget import BudgetLedger
from scripts import prepare_summary_comprehensive_comparison as original
from scripts import prepare_summary_round2 as exam
from scripts import run_summary_round2 as live
from scripts import run_summary_comprehensive_comparison as first
from scripts.prepare_summary_fidelity_comparison import encoded


@pytest.fixture
def packet():
    return {"schema": "echo-summary-private-excerpts-v1", "captured_at": "2035-01-01T00:00:00+08:00",
        "source_context": copy.deepcopy(original.PRIVATE_CONTEXT), "cases": [
            {"case_id": f"private-stub-{i}", "category": "explicit_adult_excerpt", "focus": "synthetic plumbing only",
             "records": [{"record_id": f"local-excerpt-stub-{i}", "recorded_at": None,
                          "source_kind": original.PRIVATE_KIND, "content": "[Synthetic stub] 谁送的？"}],
             "review_targets": ["question, not confirmation"], "forbidden_claims": ["invented subject"]}
            for i in range(3)]}


@pytest.fixture
def bundle(packet):
    return exam.build_bundle(packet)


def auth(bundle):
    return {"scope": live.scope_for(bundle), "user_authorized": True, "authorization_id": live.AUTHORIZATION_ID}


def final_response(bundle):
    answers = {"cases": [{"case_id": c["case_id"], "summary": {"status": "insufficient", "facts": [],
                         "uncertainties": ["Synthetic fixture only"], "refusal_reason": None}}
                         for c in bundle["writer_inputs.json"]["cases"]]}
    return {"finish": "complete", "final_content": json.dumps(answers, ensure_ascii=False)}


def fact(text="Synthetic fact", **overrides):
    return {"text": text, "event_status": "not_applicable", "statement_role": "direct",
            "event_date": None, "planned_date": None, "evidence_ids": ["s1"], "supersedes": None, **overrides}


def test_original_round_unchanged_and_cohorts_are_not_blind_scores(bundle, packet):
    old = original.build_bundle(original.load_cases(), packet)
    snapshot = copy.deepcopy(old)
    assert exam.build_bundle(packet) == bundle
    assert old == snapshot
    assert first.payload_for(old, "openai")["input"][0]["content"] != live.payload_for(bundle, "openai")["input"][0]["content"]
    groups = bundle["manifest.json"]["cohort_case_ids"]
    assert {k: len(v) for k, v in groups.items()} == {"regression": 6, "new_long_development": 3, "private_repeat": 3}
    for case in bundle["reviewer_sheet.json"]["cases"]:
        assert len(case["grades"]) == 2 and all(g["semantic_passed"] is None for g in case["grades"])
        if case["cohort"] == "new_long_development":
            assert sum(len(r["content"]) for r in case["records"]) >= 400
    assert "not_blind" in bundle["manifest.json"]["evaluation_kind"]
    public = exam.build_bundle()
    assert public["manifest.json"]["coverage_complete"] is False
    with pytest.raises(SummaryError, match="twelve_case_exam_required"):
        live.scope_for(public)


def test_both_providers_receive_identical_sources_without_grade_keys(bundle):
    gpt = live.payload_for(bundle, "openai")
    ds = live.payload_for(bundle, "deepseek")
    assert gpt["input"] == ds["messages"]
    changed = copy.deepcopy(bundle)
    for case in changed["reviewer_sheet.json"]["cases"]:
        case["review_targets"] = ["DO_NOT_SEND_GOLD_MARKER"]
    assert live.payload_for(changed, "openai") == gpt
    assert "DO_NOT_SEND_GOLD_MARKER" not in json.dumps(gpt)
    with pytest.raises(SummaryError, match="provider_not_allowed"):
        live.payload_for(bundle, "venice")
    scope = live.scope_for(bundle)
    assert sum(Decimal(v) for v in scope["reserved_usd"].values()) < live.MAX_ROUND_RESERVATION
    assert scope["calls_per_provider"] == 1 and scope["case_count"] == 12


def test_private_freeze_rejects_changed_source_or_prompt(tmp_path, monkeypatch, bundle, packet):
    monkeypatch.setattr(original, "PRIVATE_ROOT", tmp_path)
    private_file = tmp_path / "private-stub.json"
    private_file.write_bytes(encoded(packet))
    monkeypatch.setattr(original, "PRIVATE_FIXTURE", private_file)
    suite = tmp_path / "suite"
    original.prepare(suite, bundle)
    assert live.load_frozen(suite) == bundle
    with pytest.raises(FileExistsError):
        original.prepare(suite, bundle)
    altered = copy.deepcopy(bundle["writer_inputs.json"])
    altered["cases"][0]["messages"][0]["content"] += "tampered"
    (suite / "writer_inputs.json").write_bytes(encoded(altered))
    with pytest.raises(SummaryError, match="frozen_round2_changed"):
        live.load_frozen(suite)


def test_compression_counts_history_and_uncertainty_without_claiming_fidelity(bundle):
    response = final_response(bundle)
    parsed = json.loads(response["final_content"])
    row = next(r for r in parsed["cases"] if r["case_id"] == "long-daily-01")
    row["summary"] = {"status": "ok", "facts": [fact("current", statement_role="correction",
        supersedes={"text": "old", "evidence_ids": ["s1"]})], "uncertainties": ["unknown"], "refusal_reason": None}
    response["final_content"] = json.dumps(parsed)
    outcomes = live.inspect_final(response, bundle)
    item = next(o for o in outcomes if o["case_id"] == row["case_id"])
    assert item["compression"]["visible_chars"] == len("currentoldunknown")
    assert item["compression"]["structured_bytes"] > item["compression"]["visible_chars"]
    assert item["semantic_passed"] is None
    assert all(o["compression"] is None for o in outcomes if o["case_id"] != row["case_id"])


def test_long_contract_can_hold_eight_distinct_facts_without_changing_first_round(bundle):
    response = final_response(bundle)
    parsed = json.loads(response["final_content"])
    parsed["cases"][0]["summary"] = {"status": "ok", "facts": [fact(str(i)) for i in range(8)],
                                      "uncertainties": [], "refusal_reason": None}
    response["final_content"] = json.dumps(parsed)
    assert live.inspect_final(response, bundle)[0]["structural_passed"]
    assert first.inspect_final(response, bundle)[0]["structural_errors"] == ["schema_invalid"]
    parsed["cases"][0]["summary"]["facts"].append(fact("ninth"))
    response["final_content"] = json.dumps(parsed)
    assert not live.inspect_final(response, bundle)[0]["structural_passed"]


@pytest.mark.parametrize("change", ["missing", "duplicate", "truncated", "blank", "unknown_reference"])
def test_failed_answers_do_not_earn_compression_success(bundle, change):
    response = final_response(bundle)
    parsed = json.loads(response["final_content"])
    if change == "missing": parsed["cases"].pop()
    if change == "duplicate": parsed["cases"][-1] = parsed["cases"][0]
    if change == "truncated": response["finish"] = "incomplete"
    if change == "unknown_reference":
        for row in parsed["cases"]:
            row["summary"] = {"status": "ok", "facts": [fact(evidence_ids=["nonexistent"])],
                              "uncertainties": [], "refusal_reason": None}
    response["final_content"] = " " if change == "blank" else json.dumps(parsed)
    result = live.inspect_final(response, bundle)
    assert all(not o["structural_passed"] and o["compression"] is None for o in result)


def setup(tmp_path, monkeypatch, bundle, provider="openai", limit="12.00", prior="9.929364"):
    monkeypatch.setattr(original, "PRIVATE_ROOT", tmp_path)
    path = tmp_path / "ledger.json"
    path.write_bytes(encoded({"schema": "echo-summary-budget-v1", "provider": provider,
                             "limit_usd": limit, "prior_reserved_usd": prior, "reservations": []}))
    return dict(bundle=bundle, provider=provider, directory=tmp_path / "run", key_file=tmp_path / "key.dpapi",
                ledger_path=path, authorization=auth(bundle))


@pytest.mark.parametrize("problem", ["old_authorization", "modified_payload", "old_budget", "wrong_provider"])
def test_permission_and_budget_fail_before_key_read(tmp_path, monkeypatch, bundle, problem):
    args = setup(tmp_path, monkeypatch, bundle, limit="10" if problem == "old_budget" else "12")
    def no_key(*a, **k): pytest.fail("must not read credentials")
    args["key_reader"] = no_key
    if problem == "old_authorization": args["authorization"]["authorization_id"] = "liora-comprehensive-20261125"
    if problem == "modified_payload":
        message = args["bundle"]["writer_inputs.json"]["cases"][0]["messages"][1]
        content = json.loads(message["content"])
        content["records"][0]["spans"][0]["text"] += "Altered source"
        message["content"] = json.dumps(content, ensure_ascii=False)
    if problem == "wrong_provider":
        data = json.loads(args["ledger_path"].read_text())
        data["provider"] = "deepseek"
        args["ledger_path"].write_bytes(encoded(data))
    if problem in ("old_authorization", "modified_payload"):
        with pytest.raises(SummaryError, match="exact_round2_authorized_scope_required"):
            live.run(**args)
        assert not args["directory"].exists()
    else:
        report = live.run(**args)
        assert report["status"] == "stopped_no_retry" and report["model_calls_reserved"] == 0
    assert json.loads(args["ledger_path"].read_text())["reservations"] == []


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_first_attempt_preserved_and_not_resent(tmp_path, monkeypatch, bundle, provider):
    args = setup(tmp_path, monkeypatch, bundle, provider=provider,
                 limit="12" if provider == "openai" else ".05", prior="9.929364" if provider == "openai" else ".0140493")
    initial = json.loads(args["ledger_path"].read_text())
    monkeypatch.setattr(first, "preflight", lambda c: {"synthetic": True})
    reads = []
    def key_reader(*a, **k): reads.append(1); return "synthetic-key-" + "x" * 32
    class MockClient:
        def __init__(self, p, k): self.provider = p
        def request(self, endpoint, payload):
            assert len(json.loads(args["ledger_path"].read_text())["reservations"]) == 1
            assert (args["directory"] / "attempt.json").is_file()
            raise SummaryError("mock_timeout_no_retry")
    args.update(key_reader=key_reader, client_factory=MockClient)
    result = live.run(**args)
    assert result["model_calls_reserved"] == 1 and result["status"] == "stopped_no_retry"
    original_report = (args["directory"] / "report.json").read_bytes()
    args["directory"] = tmp_path / "second-directory"
    retry = live.run(**args)
    assert retry["error_code"] == "batch_already_attempted_no_automatic_retry" and len(reads) == 1
    assert (tmp_path / "run" / "report.json").read_bytes() == original_report
    after = json.loads(args["ledger_path"].read_text())
    assert after["prior_reserved_usd"] == initial["prior_reserved_usd"]
    assert after["limit_usd"] == initial["limit_usd"] and len(after["reservations"]) == 1


def test_higher_ceiling_requires_explicit_caller_and_keeps_old_spending(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_bytes(encoded({"schema": "echo-summary-budget-v1", "provider": "openai",
                             "limit_usd": "12.00", "prior_reserved_usd": "9.929364", "reservations": []}))
    with pytest.raises(SummaryError, match="invalid_existing_budget_ledger"):
        with BudgetLedger(path, maximum_limit=Decimal("10.00")):
            pytest.fail("old entry must not inherit larger scope")
    with BudgetLedger(path, maximum_limit=Decimal("12.00")) as ledger:
        assert ledger.total == Decimal("9.929364")
    with pytest.raises(SummaryError, match="invalid_trial_budget_ceiling"):
        BudgetLedger(path, maximum_limit=Decimal("20.01"))
