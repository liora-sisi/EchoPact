#!/usr/bin/env python3
"""Four fixed synthetic rooms; isolated precomputation and read-only query demo."""
from __future__ import annotations
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory.caretaker_summary import SummaryError
from backend.memory import caretaker_summary_v4 as contract
from backend.memory.summary_cabinet import Cabinet, SOURCES, digest, plan
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import (
    MODEL_PRICE_CEILINGS, TrialBudget, VeniceClient, preflight, read_dpapi_key, run_case,
)

MISTRAL = "mistral-small-3-2-24b-instruct"
FIXTURE_MODEL = "reference-fixture-not-a-model"
SCOPE = "synthetic-family"


def write_json(path, data):
    # Attempts and responses are immutable, including unusual resume numbering.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def load_fixture():
    cases = json.loads((ROOT / "tests/fixtures/summary_cabinet_synthetic.json").read_text(encoding="utf-8"))
    corpus = [{"scope_id": SCOPE, **{k: c[k] for k in ("source_namespace", "room_id", "records")}} for c in cases]
    return cases, corpus


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立摘要柜合成原型；默认仅预览，无模型调用。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--exercise-fixture", action="store_true")
    mode.add_argument("--allow-live-api", action="store_true")
    mode.add_argument("--query", type=str)
    mode.add_argument("--expand-job", type=str)
    parser.add_argument("--writer-model", choices=list(MODEL_PRICE_CEILINGS), default=MISTRAL)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    cases, corpus = load_fixture()
    model = FIXTURE_MODEL if args.exercise_fixture else args.writer_model
    provider = "fixture" if args.exercise_fixture else "venice"
    if args.query is not None or args.expand_job:
        if not args.output_dir or not (args.output_dir / "manifest.json").is_file():
            parser.error("query/expand requires an existing trial directory")
        manifest = json.loads((args.output_dir / "manifest.json").read_text(encoding="utf-8"))
        model = manifest["writer_model"]
        provider = manifest["writer_provider"]
    jobs = plan(corpus, model=model, provider=provider)
    if not (args.exercise_fixture or args.allow_live_api or args.query is not None or args.expand_job):
        print(json.dumps({"mode": "dry_run", "rooms": len(corpus), "jobs": len(jobs),
                          "writer_provider": provider, "writer_model": model,
                          "query_model": None, "model_calls": 0}))
        return 0
    if not args.output_dir:
        parser.error("output-dir required")
    identity = {"schema": "echo-summary-cabinet-run-v1", "writer_provider": provider, "writer_model": model,
                "corpus_sha256": digest(corpus), "jobs": [j["job_id"] for j in jobs], "synthetic_only": True}
    manifest_path = args.output_dir / "manifest.json"
    read_only_query = args.query is not None or bool(args.expand_job)
    if args.output_dir.exists():
        if not (args.resume or read_only_query) or not manifest_path.is_file():
            parser.error("existing directory requires explicit resume and matching manifest")
        if json.loads(manifest_path.read_text(encoding="utf-8")) != identity:
            parser.error("source/model/recipe changed; preserve old run and use a new directory")
        source_path = args.output_dir / "synthetic-source.json"
        if not source_path.is_file() or digest(json.loads(source_path.read_text(encoding="utf-8"))) != digest(corpus):
            parser.error("frozen source missing or changed; preserve artifacts for review")
        if not (args.output_dir / "summaries.sqlite3").is_file():
            parser.error("existing cabinet missing; do not recreate silently")
    else:
        if args.resume or read_only_query:
            parser.error("nothing to resume or query")
        args.output_dir.mkdir(parents=True)
        write_json(manifest_path, identity)
        write_json(args.output_dir / "synthetic-source.json", corpus)
        write_json(args.output_dir / "writer-contract.json",
                   {"prompt": contract.SYSTEM_PROMPT, "schema": contract.SUMMARY_SCHEMA})
    with Cabinet(args.output_dir / "summaries.sqlite3", read_only=read_only_query) as cabinet:
        if args.query is not None:
            result = cabinet.lookup(corpus, summary_model=model, scope_id=SCOPE,
                                    source_namespaces=sorted(SOURCES), terms=[args.query], summary_provider=provider)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.expand_job:
            result = cabinet.expand(corpus, summary_model=model, scope_id=SCOPE,
                                    source_namespaces=sorted(SOURCES), job_id=args.expand_job, summary_provider=provider)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        report = {**identity, "model_calls": 0, "production_accepted": False, "outcomes": []}
        if args.exercise_fixture:
            reference = {digest(c["records"]): c["reference_summary"] for c in cases}
            def fixture(records, *, job_id):
                return {"model": FIXTURE_MODEL, "model_summary": reference[digest(records)]}
            report["outcomes"] = cabinet.ingest(jobs, fixture)
            for job in jobs:
                if not cabinet._approved(job["job_id"]):
                    cabinet.review(job, accepted=True, reviewer="synthetic-reference-fixture",
                        note="Mechanism exercise only; fixed reference answers, not model quality evidence.")
            report["status"] = "fixture_mechanism_exercised_no_model"
        else:
            if not args.key_file or not args.budget_ledger:
                parser.error("live trial requires a protected key and existing cumulative ledger")
            try:
                with BudgetLedger(args.budget_ledger) as ledger:
                    pending = [j for j in jobs if cabinet.state(j["job_id"]) is None]
                    report.update(budget_limit_usd=str(ledger.limit), inherited_reserved_usd=str(ledger.total))
                    try:
                        if pending:
                            if any((args.output_dir / (j["job_id"] + ".response.json")).exists() for j in pending):
                                raise SummaryError("cabinet_orphan_response_requires_review")
                            client = VeniceClient(read_dpapi_key(args.key_file))
                            prices = preflight(client, model)
                            preview = TrialBudget(spent_reservations=ledger.total, limit_usd=ledger.limit, max_calls=4)
                            for job in pending:
                                preview.reserve(contract.build_messages(job["records"]), prices, contract.SUMMARY_SCHEMA)
                            report["projected_reserved_usd"] = str(preview.spent_reservations)
                            budget = TrialBudget(spent_reservations=ledger.total, limit_usd=ledger.limit, max_calls=4)
                            def generate(records, *, job_id):
                                def reserve(item):
                                    ledger.reserve(amount=Decimal(item["reserved_usd"]), run_id=args.output_dir.name,
                                                   model=model, case_id=job_id)
                                try:
                                    result = run_case(client, records, prices=prices, budget=budget,
                                        before_request=reserve, model=model, contract_version="v4")
                                except SummaryError as error:
                                    if hasattr(error, "rejected_draft"):
                                        write_json(args.output_dir / (job_id + ".response.json"),
                                            {"rejected_draft": error.rejected_draft, "usage": error.trial_usage})
                                    raise
                                write_json(args.output_dir / (job_id + ".response.json"), result)
                                return result
                            report["outcomes"] = cabinet.ingest(jobs, generate)
                            report["model_calls"] = budget.calls
                        else:
                            report["outcomes"] = [{"job_id": j["job_id"], "state": cabinet.state(j["job_id"]),
                                                   "reused": True} for j in jobs]
                        report["status"] = ("stopped_no_retry" if any(o["state"] in ("failed", "started")
                            for o in report["outcomes"]) else "drafts_require_review")
                    finally:
                        report["cumulative_reserved_usd"] = str(ledger.total)
            except SummaryError as error:
                report.update(status="stopped_no_retry", error_code=str(error))
        report_path = args.output_dir / "report.json"
        # Append run reports rather than replacing original attempts on resume.
        if report_path.exists():
            count = 1
            while (args.output_dir / f"resume-{count}.json").exists():
                count += 1
            report_path = args.output_dir / f"resume-{count}.json"
        write_json(report_path, report)
        print(json.dumps({k: report[k] for k in ("status", "model_calls", "production_accepted")}, ensure_ascii=False))
    return 0 if report["status"] != "stopped_no_retry" else 1


if __name__ == "__main__":
    raise SystemExit(main())
