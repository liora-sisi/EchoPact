"""Offline regression checks for dates, model selection and cumulative spending."""

import copy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory.caretaker_summary_v2 import build_messages, validate_summary
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import MODEL_PRICE_CEILINGS, TrialBudget, preflight, run_case
from scripts.summary_model_comparison import load_cases, restore_attempted, run_comparison

CASES = load_cases()
MODELS = list(MODEL_PRICE_CEILINGS)


def valid_summary():
    citation = {"record_id": "date-2", "quote": "修书架是2026年10月25日，26日只是买了螺丝。"}
    return {"status": "ok", "facts": [{"text": "后来更正：修书架在2026年10月25日完成。",
        "event_status": "happened", "statement_role": "correction", "event_date": "2026-10-25",
        "planned_date": None, "date_evidence": citation.copy(), "evidence": [citation]}],
        "uncertainties": ["最初把修书架说成26日，后来纠正为25日。"],
        "omitted_source_ids": ["date-1"], "refusal_reason": None}


def seed(tmp_path, prior="0.01116375"):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"schema": "echo-summary-budget-v1", "limit_usd": "0.05",
        "prior_reserved_usd": prior, "reservations": []}), encoding="utf-8")
    return path


def test_happened_and_correction_are_independent_and_original_untouched():
    records, summary = copy.deepcopy(CASES[0]["records"]), valid_summary()
    before = copy.deepcopy(records)
    result = validate_summary(summary, records)
    assert result["schema_version"].endswith("v2") and result["requires_human_review"]
    assert result["summary"]["facts"][0]["event_status"] == "happened"
    assert result["summary"]["facts"][0]["statement_role"] == "correction"
    assert records == before
    summary["facts"].clear()
    assert result["summary"]["facts"]


@pytest.mark.parametrize("change,code", [
    (lambda f: f.update(event_date="2026-10-29"), "date_not_explicit_in_quote"),
    (lambda f: f.update(event_date="2026-10-26"), "date_not_explicit_in_quote"),
    (lambda f: f.update(date_evidence=None), "date_evidence_not_cited"),
    (lambda f: f.update(event_status="planned"), "event_date_requires_happened"),
    (lambda f: f.update(event_status="cancelled", event_date="2026-10-29"), "event_date_requires_happened"),
    (lambda f: f.update(planned_date="2026-10-25"), "planned_date_requires_plan"),
    (lambda f: f.update(statement_role="planned"), "invalid_statement_role"),
    (lambda f: f.update(event_date="2026-02-30"), "invalid_event_date"),
])
def test_wrong_dates_or_mixed_labels_rejected(change, code):
    summary = valid_summary()
    change(summary["facts"][0])
    with pytest.raises(SummaryError, match=code):
        validate_summary(summary, CASES[0]["records"])


def test_cancelled_plan_uses_scheduled_date_never_cancellation_date():
    quote = {"record_id": "plan-1", "quote": CASES[1]["records"][0]["content"]}
    summary = valid_summary()
    summary["facts"][0].update(text="原定12日去海边的计划已取消。", event_status="cancelled",
        statement_role="direct", event_date=None, planned_date="2026-11-12", date_evidence=quote,
        evidence=[quote, {"record_id": "plan-2", "quote": "12日去海边的计划取消了。"}])
    summary.update(uncertainties=["取消决定的日期未记录。"], omitted_source_ids=[])
    assert validate_summary(summary, CASES[1]["records"])["summary"]["facts"][0]["event_date"] is None


def test_explicit_date_quote_does_not_prove_semantics_or_correct_wrong_event():
    # Date presence alone cannot detect borrowing another event's date. This
    # deliberately adversarial example documents why a human semantic gate remains.
    summary = valid_summary()
    summary["facts"][0]["text"] = "这一天买好了螺丝。"
    assert validate_summary(summary, CASES[0]["records"])["requires_human_review"] is True


@pytest.mark.parametrize("prior", ["NaN", "Infinity", "-1", "0.06"])
def test_invalid_inherited_budget_stops_before_any_api(tmp_path, prior):
    with pytest.raises(SummaryError, match="invalid_existing_budget_ledger"):
        with BudgetLedger(seed(tmp_path, prior)):
            pytest.fail("invalid budget accepted")


def test_budget_lock_and_persistent_reservation_survive_new_runner(tmp_path):
    path = seed(tmp_path)
    with BudgetLedger(path) as ledger:
        ledger.reserve(amount=Decimal("0.001"), run_id="one", model=MODELS[0], case_id="case")
        with pytest.raises(SummaryError, match="locked"):
            with BudgetLedger(path):
                pytest.fail("concurrent runner accepted")
    with BudgetLedger(path) as ledger:
        assert ledger.total == Decimal("0.01216375")
        with pytest.raises(SummaryError, match="budget_exhausted"):
            ledger.reserve(amount=Decimal("0.04"), run_id="two", model=MODELS[1], case_id="case")


class FakeComparisonClient:
    def __init__(self, ledger_path):
        self.calls = []
        self.ledger_path = ledger_path
        self.invalid_first = False
        self.fail_transport = False
        self.prior_calls = 0

    def request(self, endpoint, payload=None):
        if endpoint == "/api_keys/rate_limits":
            return {"data": {"accessPermitted": True}}
        if endpoint.startswith("/models"):
            return {"data": [{"id": name, "model_spec": {"privacy": "private", "offline": False,
                "capabilities": {"supportsResponseSchema": True},
                "pricing": {"input": {"usd": str(inp)}, "output": {"usd": str(out)}}}}
                for name, (inp, out) in MODEL_PRICE_CEILINGS.items()]}
        self.calls.append(payload)
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        assert len(ledger["reservations"]) == len(self.calls) + self.prior_calls  # Durable BEFORE request.
        assert ledger["reservations"][-1]["model"] == payload["model"]
        if self.fail_transport:
            raise SummaryError("provider_network_failure_no_retry")
        records = json.loads(payload["messages"][1]["content"])["synthetic_records"]
        summary = {"status": "ok", "facts": [{"text": "一项合成资料事实。", "event_status": "not_applicable",
            "statement_role": "direct", "event_date": None, "planned_date": None, "date_evidence": None,
            "evidence": [{"record_id": records[0]["record_id"], "quote": records[0]["content"]}]}],
            "uncertainties": [], "omitted_source_ids": [r["record_id"] for r in records[1:]], "refusal_reason": None}
        if self.invalid_first and len(self.calls) == 1:
            summary["facts"][0].update(event_status="happened", event_date="2026-10-29")
        return {"model": payload["model"], "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(summary), "reasoning_content": "do not keep"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 400}}


def test_same_prompt_and_inputs_both_models_with_quarantined_failure(tmp_path):
    path = seed(tmp_path)
    client = FakeComparisonClient(path)
    client.invalid_first = True
    report = {"run_id": "test", "cases": []}
    with BudgetLedger(path) as ledger:
        run_comparison(client, CASES, report, tmp_path, ledger)
        assert Decimal("0.01116375") < ledger.total < Decimal("0.05")
    assert len(client.calls) == len(report["cases"]) == 12
    assert report["cases"][0]["status"] == "rejected"
    assert report["cases"][1]["status"] == "validated_structure"
    assert report["status"] == "awaiting_human_review"
    assert "reasoning_content" not in json.dumps(report)
    for i in range(0, 12, 2):
        left, right = client.calls[i:i + 2]
        assert left["model"] != right["model"]
        assert left["messages"] == right["messages"]
        assert left["response_format"] == right["response_format"]
        assert set(json.loads(left["messages"][1]["content"])) == {"synthetic_records"}


def test_network_failure_reserved_no_retry_and_matrix_budget_checked_upfront(tmp_path):
    path = seed(tmp_path)
    client = FakeComparisonClient(path)
    client.fail_transport = True
    report = {"run_id": "test", "cases": []}
    with BudgetLedger(path) as ledger:
        with pytest.raises(SummaryError, match="network_failure_no_retry"):
            run_comparison(client, CASES, report, tmp_path, ledger)
        assert len(client.calls) == 1 and ledger.total > Decimal("0.01116375")
    path.write_text(json.dumps({"schema": "echo-summary-budget-v1", "limit_usd": "0.05",
        "prior_reserved_usd": "0.0499", "reservations": []}), encoding="utf-8")
    client.calls.clear()
    with BudgetLedger(path) as ledger:
        with pytest.raises(SummaryError, match="budget_exhausted"):
            run_comparison(client, CASES, report, tmp_path, ledger)
        assert not client.calls


def test_disallowed_model_or_mismatched_price_identity_has_no_api_calls(tmp_path):
    client = FakeComparisonClient(seed(tmp_path))
    with pytest.raises(SummaryError, match="model_not_in_trial_allowlist"):
        preflight(client, "not-approved")
    with pytest.raises(SummaryError, match="model_not_in_trial_allowlist"):
        run_case(client, CASES[0]["records"], prices={"model": MODELS[0]}, budget=TrialBudget(),
                 before_request=lambda _: None, model=MODELS[1], contract_version="v2")
    assert not client.calls


def test_comparison_cli_is_offline_by_default():
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, str(root / "scripts/summary_model_comparison.py")],
        check=True, capture_output=True, text=True, encoding="utf-8")
    result = json.loads(done.stdout)
    assert result["mode"] == "dry_run" and result["model_calls"] == 0
    assert result["planned_calls"] == 12 and len(result["models"]) == 2


def test_explicit_continuation_skips_timeout_and_preserves_old_evidence(tmp_path):
    path = seed(tmp_path)
    client = FakeComparisonClient(path)
    client.fail_transport = True
    identity = {"schema": "test-schema", "contract": "v2", "prompt_sha256": "prompt",
                "schema_sha256": "schema", "input_sha256": "inputs", "synthetic_only": True}
    previous = {**identity, "run_id": "first", "cases": []}
    with BudgetLedger(path) as ledger:
        with pytest.raises(SummaryError, match="network_failure"):
            run_comparison(client, CASES, previous, tmp_path, ledger)
    previous["status"] = "stopped_no_retry"
    continued = {**identity, "run_id": "second", "cases": []}
    with BudgetLedger(path) as ledger:
        changed = {**continued, "prompt_sha256": "changed"}
        with pytest.raises(SummaryError, match="contract_or_input_changed"):
            restore_attempted(previous, changed, CASES, ledger)
        restore_attempted(previous, continued, CASES, ledger)
        client.calls.clear()
        client.prior_calls = 1
        client.fail_transport = False
        run_comparison(client, CASES, continued, tmp_path, ledger)
    assert len(client.calls) == 11 and len(continued["cases"]) == 12
    assert continued["status"] == "awaiting_human_review_with_missing"
    assert continued["cases"][0] == previous["cases"][0]
    assert client.calls[0]["model"] == MODELS[1]


@pytest.mark.parametrize("limit", ["NaN", "Infinity", "0", "-0.1", "0.1001", 0.1])
def test_ledger_rejects_invalid_or_above_authorized_ceiling(tmp_path, limit):
    path = seed(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["limit_usd"] = limit
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SummaryError, match="invalid_existing_budget_ledger"):
        with BudgetLedger(path):
            pytest.fail("invalid limit accepted")


def test_explicit_raised_limit_keeps_prior_reservations_and_default_stays_small(tmp_path):
    path = seed(tmp_path, prior="0.04412392500")
    client = FakeComparisonClient(path)
    cases = load_cases()
    with BudgetLedger(path) as ledger:
        assert ledger.limit == Decimal("0.05")
        with pytest.raises(SummaryError, match="budget_exhausted"):
            run_comparison(client, cases, {"run_id": "old", "cases": []}, tmp_path, ledger)
        assert not client.calls and ledger.total == Decimal("0.04412392500")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["limit_usd"] = "0.10"
    path.write_text(json.dumps(data), encoding="utf-8")
    report = {"run_id": "expanded", "cases": []}
    with BudgetLedger(path) as ledger:
        run_comparison(client, cases, report, tmp_path, ledger)
        assert ledger.limit == Decimal("0.10")
        assert Decimal("0.05") < ledger.total < Decimal("0.10")
        assert ledger.data["prior_reserved_usd"] == "0.04412392500"
    assert len(client.calls) == 12 and report["budget_limit_usd"] == "0.10"
    assert TrialBudget().limit_usd == Decimal("0.05")


def test_expanded_budget_still_blocks_before_call_and_reservation(tmp_path):
    path = seed(tmp_path, prior="0.0999")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["limit_usd"] = "0.10"
    path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeComparisonClient(path)
    with BudgetLedger(path) as ledger:
        with pytest.raises(SummaryError, match="budget_exhausted"):
            ledger.reserve(amount=Decimal("0.001"), run_id="over", model=MODELS[1], case_id="one")
        with pytest.raises(SummaryError, match="budget_exhausted"):
            run_comparison(client, CASES, {"run_id": "over", "cases": []}, tmp_path, ledger)
        assert not client.calls and not ledger.data["reservations"]
    with pytest.raises(SummaryError, match="invalid_trial_budget"):
        TrialBudget(limit_usd=Decimal("0.11"))
    with pytest.raises(SummaryError, match="budget_exhausted"):
        TrialBudget(spent_reservations=Decimal("0.0999"), limit_usd=Decimal("0.10")).reserve(
            build_messages(CASES[0]["records"]), {
                "input_usd_per_million": "0.09375", "output_usd_per_million": "0.25"})


def test_offline_preview_reads_actual_limit_without_touching_ledger_or_key(tmp_path):
    path = seed(tmp_path, prior="0.04412392500")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["limit_usd"] = "0.10"
    path.write_text(json.dumps(data), encoding="utf-8")
    original = path.read_bytes()
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, str(root / "scripts/summary_model_comparison.py"),
        "--evidence-repair-probe", "--budget-ledger", str(path), "--key-file", "missing-key"],
        check=True, capture_output=True, text=True, encoding="utf-8")
    preview = json.loads(done.stdout)
    assert preview["cumulative_budget_usd"] == "0.10" and preview["model_calls"] == 0
    assert Decimal(preview["remaining_reservation_usd"]) == Decimal("0.05587607500")
    assert path.read_bytes() == original and not path.with_name(path.name + ".lock").exists()
