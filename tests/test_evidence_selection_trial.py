import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend.memory.caretaker_summary import SummaryError
from scripts import evidence_selection_trial as cli
from backend.memory.summary_trial_budget import BudgetLedger


def environment(tmp_path, monkeypatch):
    ledger = tmp_path / "budget.json"
    ledger.write_text(json.dumps({"schema": "echo-summary-budget-v1", "provider": "venice",
        "limit_usd": "0.10", "prior_reserved_usd": "0", "reservations": []}), encoding="utf-8")
    output = tmp_path / "trial"
    calls = []
    monkeypatch.setattr(cli, "read_dpapi_key", lambda *a, **k: "synthetic-key")
    monkeypatch.setattr(cli.api.venice, "preflight", lambda *a: {})
    monkeypatch.setattr(cli.api.venice, "VeniceClient", lambda _: None)
    def run_case(client, request, *, provider, frozen_request_sha256, before_request):
        before_request({"reserved_usd": "0.001"})
        calls.append(request)
        # Test fixture only; this callback is explicitly not a model.
        case = next(c for c in cli.load_cases("all") if c["query"] == request["question"])
        prepared = cli.prepare_case(case)
        gold = {r["id"]: r for r in case["records"]}
        draft = {"judgments": [{"candidate_id": cid, "relation": gold[row["record_id"]]["relation"],
            "quote": row["text"] if gold[row["record_id"]]["relation"] in ("answers", "uncertain") else ""}
            for cid, row in prepared["local"].items()], "sufficiency": case["sufficiency"], "gap": case["gap"]}
        return {"draft": draft, "estimated_usd": "0.0001"}
    monkeypatch.setattr(cli.api, "run_case", run_case)
    args = ["--output-dir", str(output), "--provider", "venice", "--partition", "development"]
    live = ["--allow-synthetic-api", "--key-file", str(tmp_path / "unused.dpapi"), "--budget-ledger", str(ledger)]
    return ledger, output, calls, args, live


def test_prepare_has_no_key_or_network_then_explicit_resume_is_immutable(tmp_path, monkeypatch):
    ledger, output, calls, args, live = environment(tmp_path, monkeypatch)
    assert cli.main(args) == 0 and not calls
    assert cli.main(args + live + ["--resume"]) == 0 and len(calls) == 4
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert all(c["quality"]["passed"] for c in report["cases"])
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    budget_before = ledger.read_bytes()
    assert cli.main(args + live + ["--resume"]) == 0 and len(calls) == 4
    assert before == {p.name: p.read_bytes() for p in output.iterdir()} and ledger.read_bytes() == budget_before


def test_exhausted_budget_stops_before_key_or_provider(tmp_path, monkeypatch):
    ledger, output, calls, args, live = environment(tmp_path, monkeypatch)
    data = json.loads(ledger.read_text())
    data["prior_reserved_usd"] = "0.09999"
    ledger.write_text(json.dumps(data))
    monkeypatch.setattr(cli, "read_dpapi_key", lambda *a, **k: pytest.fail("must not read key"))
    with pytest.raises(SummaryError, match="exhausted"):
        cli.main(args + live)
    assert not calls and not list(output.glob("*.started.json"))


def test_changed_request_does_not_resume_and_arbitrary_private_input_is_not_supported(tmp_path, monkeypatch):
    ledger, output, calls, args, live = environment(tmp_path, monkeypatch)
    cli.main(args)
    request_path = next(output.glob("*.request.json"))
    request_path.write_text("{}")
    with pytest.raises(SummaryError, match="frozen"):
        cli.main(args + live + ["--resume"])
    with pytest.raises(SystemExit):
        cli.main(args + ["--db", "private.sqlite3"])
    assert not calls


def test_started_missing_response_is_not_retried(tmp_path, monkeypatch):
    ledger, output, calls, args, live = environment(tmp_path, monkeypatch)
    cli.main(args)
    case_id = cli.load_cases("development")[0]["case_id"]
    (output / (case_id + ".started.json")).write_text('{"status":"started"}')
    cli.main(args + live + ["--resume"])
    assert len(calls) == 3
    report = json.loads((output / "report.json").read_text())
    assert not report["cases"][0]["accepted_contract"]


def test_failed_response_is_saved_without_exception_text_or_retries(tmp_path, monkeypatch):
    ledger, output, calls, args, live = environment(tmp_path, monkeypatch)
    def broken(*a, **kw):
        kw["before_request"]({"reserved_usd": "0.001"})
        calls.append(1)
        raise RuntimeError("SECRET provider body")
    monkeypatch.setattr(cli.api, "run_case", broken)
    cli.main(args + live)
    assert len(calls) == 4
    assert all("SECRET" not in p.read_text(encoding="utf-8") for p in output.iterdir())
    cli.main(args + live + ["--resume"])
    assert len(calls) == 4


def test_amended_ceiling_requires_existing_ledger_and_older_callers_stay_bounded(tmp_path):
    path = tmp_path / "budget.json"
    data = {"schema": "echo-summary-budget-v1", "provider": "openai", "limit_usd": "0.50",
            "prior_reserved_usd": "0.482272", "reservations": []}
    path.write_text(json.dumps(data))
    with BudgetLedger(path, maximum_limit=Decimal("1.00")) as ledger:
        assert ledger.limit == Decimal("0.50")
        with pytest.raises(SummaryError, match="exhausted"):
            ledger.reserve(amount=Decimal("0.468168"), run_id="test", model="test", case_id="test")
    data["limit_usd"] = "1.00"
    path.write_text(json.dumps(data))
    with pytest.raises(SummaryError, match="invalid_existing"):
        with BudgetLedger(path, maximum_limit=Decimal("0.50")):
            pytest.fail("old caller must reject higher limit")
    with BudgetLedger(path, maximum_limit=Decimal("1.00")) as ledger:
        ledger.reserve(amount=Decimal("0.468168"), run_id="test", model="test", case_id="test")
        assert ledger.total == Decimal("0.950440")
    with pytest.raises(SummaryError, match="ceiling"):
        BudgetLedger(path, maximum_limit=Decimal("2.01"))
