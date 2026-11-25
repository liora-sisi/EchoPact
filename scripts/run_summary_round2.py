#!/usr/bin/env python3
"""Second-round fixed two-provider trial; preserves earlier prompts and first answers."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory.caretaker_summary import SummaryError
from backend.memory.summary_trial_budget import BudgetLedger
from scripts import prepare_summary_comprehensive_comparison as original
from scripts import prepare_summary_round2 as exam
from scripts import run_summary_comprehensive_comparison as shared
from scripts.prepare_summary_fidelity_comparison import digest, encoded

PROVIDERS = ("openai", "deepseek")
CEILINGS = {"openai": Decimal("12.00"), "deepseek": Decimal("0.05")}
MAX_ROUND_RESERVATION = Decimal("0.50")
AUTHORIZATION_ID = "liora-summary-round2-20261125"


def load_frozen(directory):
    directory = shared.protected(directory)
    bundle = {n: json.loads((directory / n).read_text(encoding="utf-8"))
              for n in ("manifest.json", "writer_inputs.json", "reviewer_sheet.json")}
    packet = json.loads(original.PRIVATE_FIXTURE.read_text(encoding="utf-8"))
    if bundle != exam.build_bundle(packet):
        raise SummaryError("frozen_round2_changed")
    return bundle


def payload_for(bundle, provider):
    if provider not in PROVIDERS:
        raise SummaryError("round2_provider_not_allowed")
    if bundle["manifest.json"].get("contract") != exam.RECIPE:
        raise SummaryError("round2_contract_required")
    return shared.payload_for(bundle, provider)


def scope_for(bundle):
    requests = {p: payload_for(bundle, p) for p in PROVIDERS}
    amounts = {p: shared.reservation(v, p) for p, v in requests.items()}
    if sum(amounts.values()) > MAX_ROUND_RESERVATION:
        raise SummaryError("round2_batch_cap_exceeded")
    return {"schema": "echo-summary-round2-scope-v1", "recipe": exam.RECIPE,
        "manifest_sha256": digest(bundle["manifest.json"]),
        "requests_sha256": {p: digest(v) for p, v in requests.items()},
        "reserved_usd": {p: str(v) for p, v in amounts.items()},
        "maximum_total_reserved_usd": str(MAX_ROUND_RESERVATION),
        "ledger_ceiling_usd": {p: str(v) for p, v in CEILINGS.items()},
        "calls_per_provider": 1, "case_count": 12,
        "source_chars": bundle["manifest.json"]["source_chars"],
        "private_source_chars": sum(len(r["content"]) for c in bundle["reviewer_sheet.json"]["cases"]
            if c["category"] == "explicit_adult_excerpt" for r in c["records"]),
        "original_databases_read_only": True, "production_accepted": False}


def inspect_final(final, bundle):
    outcomes = shared.inspect_final(final, bundle, summary_schema=exam.SUMMARY_SCHEMA)
    by_id = {c["case_id"]: c for c in bundle["reviewer_sheet.json"]["cases"]}
    for out in outcomes:
        case = by_id[out["case_id"]]
        source = "".join(r["content"] for r in case["records"])
        out["cohort"] = case["cohort"]
        out["compression"] = None
        out["review_hints"] = []
        if out["structural_passed"] and out["summary"]["status"] == "ok":
            summary = out["summary"]
            visible = [f["text"] for f in summary["facts"]] + summary["uncertainties"]
            visible += [f["supersedes"]["text"] for f in summary["facts"] if f["supersedes"]]
            chars = sum(map(len, visible))
            ratio = chars / len(source)
            out["compression"] = {"source_chars": len(source), "visible_chars": chars,
                "visible_ratio": round(ratio, 6), "structured_bytes": len(encoded(summary)),
                "source_text_bytes": len(source.encode("utf-8")),
                "long_target_met": ratio <= 0.60 if len(source) > 400 else None,
                "not_a_semantic_pass_or_token_measure": True}
            # Overlap only suggests reviewing old statements; it cannot prove semantic identity.
            for i, fact in enumerate(summary["facts"]):
                for j, newer in enumerate(summary["facts"]):
                    history = newer["supersedes"]
                    if i != j and history and set(fact["evidence_ids"]) <= set(history["evidence_ids"]):
                        out["review_hints"].append({"code": "possible_old_claim_still_current",
                                                    "fact_index": i, "correcting_fact_index": j})
    return outcomes


def run(bundle, provider, directory, key_file, ledger_path, authorization, *,
        key_reader=shared.read_dpapi_key, client_factory=shared.Client):
    directory, key_file, ledger_path = map(shared.protected, (directory, key_file, ledger_path))
    if (not isinstance(authorization, dict)
            or set(authorization) != {"scope", "user_authorized", "authorization_id"}
            or authorization["user_authorized"] is not True
            or authorization["scope"] != scope_for(bundle)
            or authorization["authorization_id"] != AUTHORIZATION_ID):
        raise SummaryError("exact_round2_authorized_scope_required")
    payload = payload_for(bundle, provider)
    amount = shared.reservation(payload, provider)
    directory.mkdir(parents=True, exist_ok=False)
    shared.write_new(directory / "request.json", payload)
    shared.write_new(directory / "scope.json", authorization)
    report = {"provider": provider, "model": shared.PROVIDERS[provider]["model"], "recipe": exam.RECIPE,
        "status": "not_run", "model_calls_reserved": 0, "production_accepted": False,
        "payload_sha256": digest(payload), "reserved_usd": str(amount), "outcomes": []}
    run_id = AUTHORIZATION_ID + ":" + provider
    try:
        with BudgetLedger(ledger_path, maximum_limit=CEILINGS[provider]) as ledger:
            if ledger.data.get("provider") != provider:
                raise SummaryError("wrong_provider_ledger")
            if any(r.get("run_id") == run_id for r in ledger.data["reservations"]):
                raise SummaryError("batch_already_attempted_no_automatic_retry")
            if ledger.total + amount > ledger.limit:
                raise SummaryError("trial_budget_exhausted")
            client = client_factory(provider, key_reader(key_file, provider=provider))
            report["preflight"] = shared.preflight(client)
            ledger.reserve(amount=amount, run_id=run_id, model=report["model"], case_id="round2-twelve-cases-v1")
            report["model_calls_reserved"] = 1
            shared.write_new(directory / "attempt.json", {"run_id": run_id,
                "reserved_usd": str(amount), "payload_sha256": digest(payload), "no_automatic_retry": True})
            started = time.monotonic()
            response = client.request(shared.PROVIDERS[provider]["endpoint"], payload)
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            final = shared.final_only(response, provider)
            shared.write_new(directory / "final-response.json", final)
            report.update(outcomes=inspect_final(final, bundle), usage=final["usage"],
                estimated_usd_at_verified_ceiling=final["estimated_usd_at_verified_ceiling"],
                status="answered_requires_semantic_review", cumulative_reserved_usd=str(ledger.total))
    except SummaryError as error:
        report.update(status="stopped_no_retry", error_code=str(error))
    except Exception:
        report.update(status="stopped_no_retry", error_code="unexpected_failure_details_suppressed")
    shared.write_new(directory / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path, required=True)
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--allow-live-api", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    args = parser.parse_args(argv)
    if args.allow_live_api and not all((args.provider, args.authorization, args.output_dir, args.key_file, args.budget_ledger)):
        parser.error("live comparison requires exact scope, new private output, key and existing ledger")
    try:
        bundle = load_frozen(args.suite_dir)
        if not args.allow_live_api:
            print(json.dumps({"status": "dry_run", "model_calls": 0, "scope": scope_for(bundle)}))
            return 0
        auth = json.loads(shared.protected(args.authorization).read_text(encoding="utf-8"))
        report = run(bundle, args.provider, args.output_dir, args.key_file, args.budget_ledger, auth)
        print(json.dumps({k: report[k] for k in ("provider", "status", "model_calls_reserved", "production_accepted")}
                         | {"error_code": report.get("error_code"), "cases_returned": len(report["outcomes"])}))
        return int(report["status"] == "stopped_no_retry")
    except (OSError, ValueError, KeyError):
        print(json.dumps({"status": "error", "code": "round2_precondition_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
