#!/usr/bin/env python3
"""Same four synthetic rooms as Venice; default offline, live opt-in and bounded."""
from __future__ import annotations
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.memory.caretaker_summary import SummaryError
from backend.memory import openai_summary_trial as api
from backend.memory.summary_cabinet import Cabinet, digest, plan
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import read_dpapi_key
from scripts.summary_cabinet_trial import load_fixture, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="OpenAI 同题对照；默认离线计划，仅发送固定合成资料。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--allow-live-api", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    _, corpus = load_fixture()
    jobs = plan(corpus, model=api.MODEL, provider="openai")
    reserve_all = sum((api.reservation(j["records"]) for j in jobs), Decimal(0))
    if len(jobs) != 4 or reserve_all > api.MAX_BUDGET_USD:
        raise SummaryError("comparison_scope_or_budget_changed")
    if not args.prepare and not args.allow_live_api:
        print(json.dumps({"status":"dry_run", "model":api.MODEL, "cases":len(jobs),
            "model_calls":0, "projected_reserved_usd":str(reserve_all),
            "maximum_budget_usd":str(api.MAX_BUDGET_USD)}))
        return 0
    if not args.output_dir:
        parser.error("output-dir required")
    if args.allow_live_api and (not args.key_file or not args.key_file.is_file() or not args.budget_ledger):
        parser.error("live comparison requires a protected OpenAI key and existing dedicated ledger")
    manifest = {"schema":"echo-summary-cabinet-run-v1", "writer_provider":"openai", "writer_model":api.MODEL,
        "corpus_sha256":digest(corpus), "jobs":[j["job_id"] for j in jobs], "synthetic_only":True}
    if args.output_dir.exists():
        if not args.resume:
            parser.error("existing comparison requires explicit resume; artifacts are never overwritten")
        try:
            if json.loads((args.output_dir/"manifest.json").read_text(encoding="utf-8")) != manifest:
                raise ValueError
            if json.loads((args.output_dir/"synthetic-source.json").read_text(encoding="utf-8")) != corpus:
                raise ValueError
            for job in jobs:
                if json.loads((args.output_dir/(job["job_id"]+".request.json")).read_text(encoding="utf-8")) != api.request_payload(job["records"]):
                    raise ValueError
            if not (args.output_dir/"summaries.sqlite3").is_file():
                raise ValueError
        except (ValueError, OSError):
            parser.error("frozen comparison changed or incomplete; preserve artifacts for review")
    else:
        if args.resume:
            parser.error("nothing to resume")
        args.output_dir.mkdir(parents=True)
        write_json(args.output_dir/"manifest.json", manifest)
        write_json(args.output_dir/"synthetic-source.json", corpus)
        for job in jobs:
            write_json(args.output_dir/(job["job_id"]+".request.json"), api.request_payload(job["records"]))
    with Cabinet(args.output_dir/"summaries.sqlite3") as cabinet:
        if args.prepare:
            print(json.dumps({"status":"prepared_no_api", "cases":len(jobs), "model_calls":0,
                              "projected_reserved_usd":str(reserve_all)}))
            return 0
        report = {**manifest, "status":"stopped_no_retry", "production_accepted":False,
                  "model_calls_reserved":0, "outcomes":[]}
        try:
            with BudgetLedger(args.budget_ledger, maximum_limit=api.MAX_BUDGET_USD) as ledger:
                if (ledger.data.get("provider") != "openai"
                        or ledger.data.get("trial_id") != "openai-summary-comparison-20261110"):
                    raise SummaryError("dedicated_openai_ledger_required")
                initial_count = len(ledger.data["reservations"])
                pending = [j for j in jobs if cabinet.state(j["job_id"]) is None]
                try:
                    if pending:
                        if any((args.output_dir/(j["job_id"]+".response.json")).exists() for j in pending):
                            raise SummaryError("cabinet_orphan_response_requires_review")
                        projected = ledger.total + sum((api.reservation(j["records"]) for j in pending), Decimal(0))
                        if projected > ledger.limit:
                            raise SummaryError("trial_budget_exhausted")
                        client = api.OpenAIClient(read_dpapi_key(args.key_file, provider="openai"))
                        report["preflight"] = api.preflight(client)
                        def generate(records, *, job_id):
                            def reserve(item):
                                ledger.reserve(amount=Decimal(item["reserved_usd"]), run_id=args.output_dir.name,
                                               model=api.MODEL, case_id=job_id)
                            try:
                                result = api.run_case(client, records, before_request=reserve)
                            except SummaryError as error:
                                if hasattr(error, "rejected_draft"):
                                    write_json(args.output_dir/(job_id+".response.json"),
                                        {"rejected_draft":error.rejected_draft,"usage":error.trial_usage})
                                raise
                            write_json(args.output_dir/(job_id+".response.json"), result)
                            return result
                        report["outcomes"] = cabinet.ingest(jobs, generate)
                    else:
                        report["outcomes"] = [{"job_id":j["job_id"],"state":cabinet.state(j["job_id"]),
                                               "reused":True} for j in jobs]
                    report["status"] = ("stopped_no_retry" if any(o["state"] in ("failed","started")
                        for o in report["outcomes"]) else "drafts_require_review")
                finally:
                    report["model_calls_reserved"] = len(ledger.data["reservations"]) - initial_count
                    report["cumulative_reserved_usd"] = str(ledger.total)
                    report["budget_limit_usd"] = str(ledger.limit)
        except SummaryError as error:
            report["error_code"] = str(error)
        except Exception:
            report["error_code"] = "unexpected_failure_details_suppressed"
        target = args.output_dir/"report.json"
        index = 1
        while target.exists():
            target = args.output_dir/f"resume-{index}.json"
            index += 1
        write_json(target, report)
        print(json.dumps({k:report[k] for k in ("status","model_calls_reserved","production_accepted")}))
        return int(report["status"] == "stopped_no_retry")


if __name__ == "__main__":
    raise SystemExit(main())
