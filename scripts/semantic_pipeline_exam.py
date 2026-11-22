"""Fixed fictional full-pipeline trial: real local retrieval, three model stages.

No frozen hit IDs, source offsets or reference answers enter the pipeline.
Only the checked-in fictional corpus can enter this optional API command.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import socket
from unittest.mock import patch

from backend.memory import semantic_caretaker_pipeline as core
from backend.memory import openai_query_trial as planner_api
from backend.memory import semantic_shortlist_trial as shortlist_api
from backend.memory import object_evidence_review_trial as review_api
from backend.memory import evidence_packet as codec
from backend.memory.semantic_shadow import LocalEncoder, build_index, file_digest
from backend.memory.identity import register_agent
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import read_dpapi_key
from scripts.assistant_recall_baseline import _private, implementation
from scripts.semantic_ordered_exam import source_records
from scripts.semantic_window_exam import write

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/semantic_pipeline_exam.json"
OWNER = "semantic-pipeline-synthetic-owner"
CEILINGS = {"planner": Decimal("0.05"), "shortlist": Decimal("0.10"), "review": Decimal("0.20")}
MAX_BATCH = Decimal("1.05")


def fingerprints():
    result = implementation()
    for name in ("semantic_pipeline_exam.py", "semantic_ordered_exam.py", "semantic_window_exam.py"):
        result["source_files_sha256"]["scripts/" + name] = file_digest(ROOT / "scripts" / name)
    return result


def verify_request(request, case, fixture, stage):
    if stage == "planner":
        if request != case["query"]:
            raise ValueError("frozen_question_changed")
        planner_api.request_payload(request)
        return
    adapter = shortlist_api if stage == "shortlist" else review_api
    adapter.request_payload(request, "openai")  # Reject unapproved fields first.
    if request["question"] != case["query"]:
        raise ValueError("frozen_question_changed")
    sources = source_records(fixture)
    for card in request["candidates"]:
        if not card["text"] or not any(card["source_kind"] == row["source_kind"] and card["role"] == row["role"]
                                      and card["text"] in row["content"] for row in sources):
            raise ValueError("request_not_frozen_fiction")


def prepare(directory, model, *, encoder=None):
    directory = _private(directory)
    directory.mkdir(parents=True, exist_ok=False)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    write(directory / "frozen-fixture.json", fixture)
    package, db, index = directory / "synthetic.json", directory / "synthetic.sqlite3", directory / "semantic.sqlite3"
    write(package, {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": source_records(fixture)})
    register_agent(OWNER, "Synthetic pipeline", actor="synthetic-pipeline", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id=OWNER, actor="synthetic-pipeline")
    with patch.object(socket.socket, "connect", side_effect=RuntimeError("offline preparation")):
        encoder = encoder or LocalEncoder(model)
        index_report = build_index(db, OWNER, index, encoder)
        pipeline = core.SemanticCaretaker(db, OWNER, index, encoder)
        for case in fixture["cases"]:
            if "query" in case:
                result = pipeline.run(case["query"], as_of=fixture["reference_at"])
                write(directory / (case["id"] + ".baseline.json"), result)
        for i, query in enumerate(fixture["controls"]):
            result = pipeline.run(query, as_of=fixture["reference_at"], planner=lambda _: (_ for _ in ()).throw(
                RuntimeError("guard failed")))
            if result["trace"]["model_calls"] or result["trace"]["retrieval_operations"] != 1:
                raise ValueError("constraint_bypass_failed")
            write(directory / ("control-" + str(i) + ".json"), result)
    cases = [case["id"] for case in fixture["cases"] if "query" in case]
    if len(cases) * sum(CEILINGS.values()) > MAX_BATCH:
        raise ValueError("too_many_cases")
    plan = {"schema": "semantic-pipeline-exam-v1", "cases": cases, "fixture_sha256": file_digest(FIXTURE),
        "source_sha256": file_digest(db), "index_sha256": file_digest(index), "model_fingerprint": encoder.fingerprint,
        "implementation": fingerprints(), "index": index_report, "max_reserved_usd": str(MAX_BATCH),
        "ceilings_usd": {k: str(v) for k, v in CEILINGS.items()}, "real_archive_records_sent": 0,
        "scope": "three_fictional_development_cases_actual_retrieval_not_production_accuracy"}
    write(directory / "plan.json", plan)
    return plan


def score(case, result):
    brief = codec.expand(result["brief"])
    items = brief.get("items", [])
    selected = {r["record_id"] for item in items for r in item.get("references", [])}
    visible = items + [i for group in brief.get("source_order_context", []) for i in group["items"]]
    targets = [{"id": target["id"], "present": any(
        any(ref["record_id"] == target["id"] for ref in item.get("references", []))
        and all(phrase in item["text"] for phrase in target["phrases"]) for item in visible)}
        for target in case["expected"]["targets"]]
    expected_kind = case["expected"]["kind"]
    checks = {"contract": result["status"] == "reviewed_requires_acceptance",
        "targets_visible": all(t["present"] for t in targets),
        "superseded_not_answer": not bool(selected & set(case["expected"]["forbidden"])),
        "sufficiency": brief.get("model_sufficiency") == expected_kind,
        "gap": expected_kind != "insufficient" or "missing_answer" in brief.get("gaps", []),
        "bounds": result["trace"]["model_calls"] <= 3 and result["trace"]["retrieval_operations"] <= 6
                  and len(core.selection.encoded(result["brief"])) <= 12000}
    return {"passed": all(checks.values()), "checks": checks, "targets": targets,
            "brief_bytes": len(core.selection.encoded(result["brief"]))}


def run_model(directory, model, key_file, ledger_path):
    directory = _private(directory)
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    db, index = directory / "synthetic.sqlite3", directory / "semantic.sqlite3"
    if (plan["fixture_sha256"] != file_digest(FIXTURE) or plan["implementation"] != fingerprints()
            or plan["source_sha256"] != file_digest(db) or plan["index_sha256"] != file_digest(index)
            or plan["max_reserved_usd"] != str(MAX_BATCH)
            or plan["ceilings_usd"] != {k: str(v) for k, v in CEILINGS.items()}):
        raise ValueError("frozen_trial_changed")
    encoder = LocalEncoder(model)
    if encoder.fingerprint != plan["model_fingerprint"]:
        raise ValueError("frozen_embedding_changed")
    pipeline = core.SemanticCaretaker(db, OWNER, index, encoder)
    report = {"scope": plan["scope"], "model": review_api.model("openai"), "new_model_calls": 0,
        "real_archive_records_sent": 0, "production_accepted": False, "cases": [], "estimated_usd": "0"}
    with BudgetLedger(_private(ledger_path), maximum_limit=Decimal("10.00")) as ledger:
        if ledger.data.get("provider") != "openai" or ledger.total + MAX_BATCH > ledger.limit:
            raise ValueError("existing_budget_unavailable")
        write(directory / "model-started.json", {"status": "started_no_automatic_retry", "max_reserved_usd": str(MAX_BATCH)})
        key = read_dpapi_key(_private(key_file), provider="openai")
        client = review_api.openai.OpenAIClient(key)
        del key
        review_api.openai.preflight(client)
        before = ledger.total
        for case in fixture["cases"]:
            if "query" not in case:
                continue
            seen = set()
            def invoke(stage, request):
                if stage in seen:
                    raise ValueError("duplicate_stage")
                seen.add(stage)
                verify_request(request, case, fixture, stage)
                adapter = {"planner": planner_api, "shortlist": shortlist_api, "review": review_api}[stage]
                payload = adapter.request_payload(request) if stage == "planner" else adapter.request_payload(request, "openai")
                payload_sha = core.selection.digest(core.selection.encoded(payload).decode())
                write(directory / (case["id"] + "." + stage + ".request.json"),
                      {"request": request, "payload_sha256": payload_sha, "max_reserved_usd": str(CEILINGS[stage]),
                       "derivation": "frozen_fiction_actual_retrieval_and_validated_prior_stage"})
                def reserve(item):
                    amount = Decimal(item["reserved_usd"])
                    if amount > CEILINGS[stage] or ledger.total - before + amount > MAX_BATCH:
                        raise ValueError("call_or_batch_ceiling_exceeded")
                    ledger.reserve(amount=amount, run_id=directory.name, model=report["model"], case_id=case["id"] + "." + stage)
                    report["new_model_calls"] += 1
                response = (planner_api.run_query(client, request, before_request=reserve) if stage == "planner"
                    else adapter.run_case(client, request, provider="openai", frozen_request_sha256=payload_sha, before_request=reserve))
                write(directory / (case["id"] + "." + stage + ".response.json"), response)
                report["estimated_usd"] = str(Decimal(report["estimated_usd"]) + Decimal(response.get("estimated_usd", "0")))
                return response["plan" if stage == "planner" else "draft"]
            result = pipeline.run(case["query"], as_of=fixture["reference_at"],
                planner=lambda q: invoke("planner", q), shortlister=lambda q: invoke("shortlist", q),
                reviewer=lambda q: invoke("review", q))
            write(directory / (case["id"] + ".result.json"), result)
            entry = {"id": case["id"], "status": result["status"], "trace": result["trace"], **score(case, result)}
            report["cases"].append(entry)
            print(json.dumps(entry), flush=True)
        report.update(reserved_usd=str(ledger.total-before), cumulative_reserved_usd=str(ledger.total), budget_limit_usd=str(ledger.limit))
    pipeline.bridge.base.check_snapshot()
    if plan["implementation"] != fingerprints() or plan["index_sha256"] != file_digest(index):
        raise ValueError("implementation_or_index_changed_during_trial")
    write(directory / "model-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run-model"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(args.output, args.model)
        print(json.dumps({"status": "prepared_offline", "cases": result["cases"], "max_reserved_usd": result["max_reserved_usd"]}))
    else:
        if not args.key_file or not args.budget_ledger:
            parser.error("explicit key and existing ledger required")
        result = run_model(args.output, args.model, args.key_file, args.budget_ledger)
        print(json.dumps({k: result[k] for k in ("new_model_calls", "reserved_usd", "estimated_usd", "cumulative_reserved_usd")}))


if __name__ == "__main__":
    main()
