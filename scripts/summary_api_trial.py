#!/usr/bin/env python3
"""Run exactly the checked-in synthetic suite; no database or arbitrary input option."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.memory.caretaker_summary import SummaryError, validate_records
from backend.memory.venice_summary_trial import (
    MODEL, TrialBudget, VeniceClient, preflight, read_dpapi_key, run_case,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Echo 合成资料摘要试验；真实 API 默认关闭。")
    parser.add_argument("--allow-live-api", action="store_true")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    cases = json.loads((REPO_ROOT / "tests/fixtures/caretaker_summary_synthetic.json").read_text(encoding="utf-8"))
    for case in cases:
        validate_records(case["records"])
    if not args.allow_live_api:
        print(json.dumps({"mode": "dry_run", "model": MODEL, "cases": len(cases),
                          "model_calls": 0, "budget_usd": "0.05"}, ensure_ascii=False))
        return 0
    if not args.key_file or not args.output_dir:
        parser.error("live trial requires --key-file and a new --output-dir")
    if args.output_dir.exists():
        parser.error("output directory must be new; retries are never implicit")
    args.output_dir.mkdir(parents=True)
    # Freeze the synthetic source separately; no summary replaces this file.
    (args.output_dir / "synthetic-input.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"schema": "echo-venice-synthetic-trial-v1", "synthetic_only": True,
              "model": MODEL, "budget_usd": "0.05", "cases": [], "requires_human_review": True}

    def receipt(case_id, item):
        entry = {"case_id": case_id, "at": datetime.now(timezone.utc).isoformat(), **item}
        with (args.output_dir / "ledger.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    failure = False
    budget = TrialBudget()
    try:
        client = VeniceClient(read_dpapi_key(args.key_file))
        prices = preflight(client)
        report["preflight"] = prices
        print(json.dumps({"preflight": "passed", **prices}, ensure_ascii=False), flush=True)
        for case in cases:
            case_id = case["case_id"]
            try:
                result = run_case(client, case["records"], prices=prices, budget=budget,
                                  before_request=lambda item: receipt(case_id, item))
                row = {"case_id": case_id, "status": "validated_structure", "result": result,
                       "review_targets": case["review_targets"]}
                receipt(case_id, {"status": "received", "estimated_usd": result["estimated_usd"]})
                print(json.dumps({"case_id": case_id, "status": row["status"],
                                  "estimated_usd": result["estimated_usd"]}), flush=True)
            except SummaryError as error:
                failure = True
                row = {"case_id": case_id, "status": "failed", "error_code": str(error),
                       "originals_preserved": True}
                if hasattr(error, "rejected_draft"):
                    row["rejected_draft"] = error.rejected_draft
                    row["usage"] = error.trial_usage
                receipt(case_id, {"status": "failed_no_retry", "error_code": str(error)})
                print(json.dumps(row), flush=True)
            report["cases"].append(row)
            # Stop on API transport failures, authorization, incomplete output or invalid evidence.
            # Continuing would risk spending money on the same unresolved failure.
            if failure:
                break
    except SummaryError as error:
        failure = True
        report["error_code"] = str(error)
    except Exception:
        # Never serialize provider objects, request headers, tracebacks or credential-bearing locals.
        failure = True
        report["error_code"] = "unexpected_trial_failure_details_suppressed"
    report["model_calls_reserved"] = budget.calls
    report["reserved_usd"] = str(budget.spent_reservations)
    report["status"] = "needs_attention" if failure else "awaiting_human_review"
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "model_calls_reserved": budget.calls,
                      "reserved_usd": report["reserved_usd"], "error_code": report.get("error_code")}), flush=True)
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
