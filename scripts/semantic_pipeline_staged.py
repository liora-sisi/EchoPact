"""Explicit private acceptance: freeze each stage before optional OpenAI calls.

At most three questions. Local replay uses the original saved model responses,
not new calls. This is not a production entrypoint or an exhaustive benchmark.
"""
from __future__ import annotations

import argparse
import copy
from decimal import Decimal
import json
from pathlib import Path
import re
import socket
from unittest.mock import patch

from backend.memory import semantic_caretaker_pipeline as core
from backend.memory import semantic_shortlist as short
from backend.memory import openai_query_trial as plan_api
from backend.memory import semantic_shortlist_trial as short_api
from backend.memory import object_evidence_review_trial as review_api
from backend.memory import evidence_packet as codec
from backend.memory.semantic_shadow import LocalEncoder, file_digest, canonical_hash
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import read_dpapi_key
from scripts.assistant_recall_baseline import _private, implementation, validate_targets
from scripts.semantic_window_exam import write

CEILINGS = {"plan": Decimal("0.05"), "shortlist": Decimal("0.10"), "review": Decimal("0.20")}
MAX_BATCH = Decimal("1.05")
ADAPTERS = {"plan": plan_api, "shortlist": short_api, "review": review_api}


def fingerprints():
    value = implementation()
    value["source_files_sha256"]["scripts/semantic_pipeline_staged.py"] = file_digest(__file__)
    value["source_files_sha256"]["scripts/semantic_window_exam.py"] = file_digest(Path(__file__).with_name("semantic_window_exam.py"))
    return value


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_scope(scope):
    fields = {"db_path", "source_sha256", "agent_id", "index_path", "index_sha256", "reference_at",
              "cases", "controls", "source_scope"}
    if not isinstance(scope, dict) or set(scope) != fields or not isinstance(scope["cases"], list) or not 1 <= len(scope["cases"]) <= 3:
        raise ValueError("invalid_scope")
    if not isinstance(scope["controls"], list) or len(scope["controls"]) > 4:
        raise ValueError("invalid_controls")
    seen = set()
    for case in scope["cases"]:
        cid = case["case_id"]
        if not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", cid) or cid in seen:
            raise ValueError("invalid_case_id")
        seen.add(cid)
        core.planning.validate_question(case["query"])
        if core.planning.assess_question(case["query"], scope["reference_at"])["decision"] != "eligible":
            raise ValueError("model_case_requires_eligible_question")
    for q in scope["controls"]:
        core.planning.validate_question(q)
    for field, digest in (("db_path", "source_sha256"), ("index_path", "index_sha256")):
        path = _private(Path(scope[field]))
        if file_digest(path) != scope[digest]:
            raise ValueError("frozen_source_or_index_changed")
    if Path(scope["db_path"] + "-wal").exists():
        raise ValueError("frozen_source_required")
    validate_targets(scope["db_path"], scope, scope["agent_id"])


def checked(directory):
    directory = _private(Path(directory))
    scope, plan = read(directory / "scope.private.json"), read(directory / "plan.json")
    if canonical_hash(scope) != plan["scope_sha256"] or fingerprints() != plan["implementation"]:
        raise ValueError("frozen_acceptance_changed")
    validate_scope(scope)
    return directory, scope, plan


def payload(stage, request):
    return plan_api.request_payload(request) if stage == "plan" else ADAPTERS[stage].request_payload(request, "openai")


def freeze_requests(directory, stage, requests):
    entries = []
    for cid, request in requests:
        value = payload(stage, request)
        amount = plan_api.reservation(request) if stage == "plan" else ADAPTERS[stage].reservation(request, "openai")
        if amount > CEILINGS[stage]:
            raise ValueError("stage_request_exceeds_ceiling")
        entries.append({"case_id": cid, "request": copy.deepcopy(request), "reserved_usd": str(amount),
                        "payload_sha256": core.selection.digest(core.selection.encoded(value).decode())})
    if len(entries) > 3 or len({e["case_id"] for e in entries}) != len(entries):
        raise ValueError("invalid_request_batch")
    manifest = {"stage": stage, "entries": entries, "reserved_usd": str(sum((Decimal(e["reserved_usd"]) for e in entries), Decimal(0)))}
    manifest["sha256"] = canonical_hash(manifest)
    write(directory / (stage + ".requests.private.json"), manifest)
    return {"stage": stage, "requests": len(entries), "reserved_usd": manifest["reserved_usd"],
            "candidate_cards": sum(len(e["request"].get("candidates", [])) for e in entries) if stage != "plan" else 0,
            "candidate_characters": sum(len(c["text"]) for e in entries for c in e["request"].get("candidates", [])) if stage != "plan" else 0}


def requests(directory, stage):
    manifest = read(directory / (stage + ".requests.private.json"))
    value = copy.deepcopy(manifest)
    digest = value.pop("sha256")
    if digest != canonical_hash(value) or manifest["stage"] != stage or len(manifest["entries"]) > 3:
        raise ValueError("frozen_stage_changed")
    for entry in manifest["entries"]:
        actual = core.selection.digest(core.selection.encoded(payload(stage, entry["request"])).decode())
        if actual != entry["payload_sha256"]:
            raise ValueError("frozen_payload_changed")
    return {entry["case_id"]: entry for entry in manifest["entries"]}


def prepare(scope_path, directory):
    scope = read(_private(Path(scope_path)))
    validate_scope(scope)
    directory = _private(Path(directory))
    directory.mkdir(parents=True, exist_ok=False)
    write(directory / "scope.private.json", scope)
    write(directory / "plan.json", {"scope_sha256": canonical_hash(scope), "implementation": fingerprints(),
          "source_scope": scope["source_scope"], "max_reserved_usd": str(len(scope["cases"]) * sum(CEILINGS.values())),
          "max_model_calls": 3 * len(scope["cases"]), "max_source_characters": 9600 * len(scope["cases"]),
          "production_accepted": False})
    return freeze_requests(directory, "plan", [(c["case_id"], c["query"]) for c in scope["cases"]])


def saved_callback(directory, stage, cid):
    entry = requests(directory, stage).get(cid)
    def callback(request):
        if entry is None or request != entry["request"]:
            raise ValueError("replay_request_changed")
        response = read(directory / (cid + "." + stage + ".response.private.json"))
        if response.get("accepted") is not True:
            raise ValueError("saved_stage_failed")
        return response["response"]["plan" if stage == "plan" else "draft"]
    return callback


def prepare_next(directory, model, stage, *, encoder=None):
    directory, scope, _ = checked(directory)
    pending = []
    with patch.object(socket.socket, "connect", side_effect=RuntimeError("offline acceptance preparation")):
        if stage == "shortlist":
            encoder = encoder or LocalEncoder(model)
            pipe = core.SemanticCaretaker(scope["db_path"], scope["agent_id"], scope["index_path"], encoder)
        else:
            bridge = short.ShortlistBridge(scope["db_path"], scope["agent_id"], source_sha256=scope["source_sha256"])
        for case in scope["cases"]:
            cid = case["case_id"]
            captured = []
            def capture(request):
                payload(stage, request)
                captured.append(copy.deepcopy(request))
                raise ValueError("frozen_capture_only")
            if stage == "shortlist":
                result = pipe.run(case["query"], as_of=scope["reference_at"], planner=saved_callback(directory, "plan", cid),
                                  shortlister=capture)
            else:
                prior = read(directory / (cid + ".shortlist-preparation.private.json"))
                if "selection" not in prior["audit"]:
                    continue
                seed = prior["audit"]["selection"]["audit"]["seed"]
                result = bridge.run(seed, shortlister=saved_callback(directory, "shortlist", cid), reviewer=capture,
                    context_seed_builder=lambda focused: core.restore_selected_windows(focused, prior["audit"]["catalog"]))
            if len(captured) > 1:
                raise ValueError("multiple_stage_callbacks")
            if captured:
                pending.append((cid, captured[0]))
            write(directory / (cid + "." + stage + "-preparation.private.json"), result)
    return freeze_requests(directory, stage, pending)


def run_phase(directory, stage, key_file, ledger_path):
    directory, scope, _ = checked(directory)
    work = requests(directory, stage)
    known = {c["case_id"]: c for c in scope["cases"]}
    if set(work) - set(known) or any((e["request"] if stage == "plan" else e["request"]["question"]) != known[cid]["query"] for cid, e in work.items()):
        raise ValueError("stage_outside_scope")
    if stage != "plan":
        bridge = short.ShortlistBridge(scope["db_path"], scope["agent_id"], source_sha256=scope["source_sha256"])
        for cid, entry in work.items():
            prepared = read(directory / (cid + "." + stage + "-preparation.private.json"))
            source = prepared["audit"]["selection"]["audit"]["seed"] if stage == "shortlist" else prepared["audit"]["focused"]
            (bridge.base if stage == "shortlist" else bridge.ordered).verify(source)
            if entry["request"] != source["request"]:
                raise ValueError("frozen_request_not_verified_source")
    report = {"stage": stage, "cases": [], "new_model_calls": 0, "estimated_usd": "0"}
    with BudgetLedger(_private(Path(ledger_path)), maximum_limit=Decimal("10.00")) as ledger:
        maximum = sum((Decimal(e["reserved_usd"]) for e in work.values()), Decimal(0))
        if ledger.data.get("provider") != "openai" or ledger.total + maximum > ledger.limit:
            raise ValueError("existing_budget_unavailable")
        if stage == "plan" and ledger.total + len(scope["cases"]) * sum(CEILINGS.values()) > ledger.limit:
            raise ValueError("whole_trial_budget_unavailable")
        write(directory / (stage + ".started.json"), {"status": "started_no_automatic_retry", "scope_sha256": canonical_hash(scope)})
        if work:
            key = read_dpapi_key(_private(Path(key_file)), provider="openai")
            client = review_api.openai.OpenAIClient(key)
            del key
            review_api.openai.preflight(client)
        before = ledger.total
        for cid, entry in work.items():
            amount = Decimal(entry["reserved_usd"])
            def reserve(item):
                if Decimal(item["reserved_usd"]) != amount or amount > CEILINGS[stage]:
                    raise ValueError("frozen_reservation_changed")
                ledger.reserve(amount=amount, run_id=directory.name, model=review_api.model("openai"), case_id=cid + "." + stage)
                report["new_model_calls"] += 1
            try:
                response = (plan_api.run_query(client, entry["request"], before_request=reserve) if stage == "plan" else
                    ADAPTERS[stage].run_case(client, entry["request"], provider="openai", frozen_request_sha256=entry["payload_sha256"], before_request=reserve))
                saved = {"accepted": True, "response": response}
                report["estimated_usd"] = str(Decimal(report["estimated_usd"]) + Decimal(response.get("estimated_usd", "0")))
            except Exception as error:
                saved = {"accepted": False, "error_type": type(error).__name__}
            write(directory / (cid + "." + stage + ".response.private.json"), saved)
            report["cases"].append({"case_id": cid, "accepted": saved["accepted"]})
        report.update(reserved_usd=str(ledger.total-before), cumulative_reserved_usd=str(ledger.total))
    checked(directory)
    write(directory / (stage + ".report.json"), report)
    return report


def target_score(case, result):
    plain = codec.expand(result["brief"])
    visible = plain.get("items", []) + [i for b in plain.get("source_order_context", []) for i in b["items"]]
    return [{"record_id": target["record_id"], "quote_visible": any(target["quote"] in item["text"] and
             any(ref["record_id"] == target["record_id"] for ref in item.get("references", [])) for item in visible)}
            for target in case["targets"]]


def evaluate(directory, model, *, encoder=None):
    directory, scope, _ = checked(directory)
    report = {"cases": [], "controls": [], "new_model_calls": 0, "production_accepted": False,
              "scope": scope["source_scope"], "semantic_answer_acceptance": "manual_review_required"}
    with patch.object(socket.socket, "connect", side_effect=RuntimeError("offline saved-response replay")):
        encoder = encoder or LocalEncoder(model)
        pipe = core.SemanticCaretaker(scope["db_path"], scope["agent_id"], scope["index_path"], encoder)
        for case in scope["cases"]:
            cid = case["case_id"]
            result = pipe.run(case["query"], as_of=scope["reference_at"], planner=saved_callback(directory, "plan", cid),
                shortlister=saved_callback(directory, "shortlist", cid), reviewer=saved_callback(directory, "review", cid))
            baseline = pipe.bridge.base.gateway.recall({"query": case["query"], "as_of": scope["reference_at"], "limit": 10, "detail": "brief"})
            write(directory / (cid + ".result.private.json"), result)
            write(directory / (cid + ".baseline-brief.private.json"), baseline)
            entry = {"case_id": cid, "status": result["status"], "targets": target_score(case, result),
                "baseline_brief_bytes": len(core.selection.encoded(baseline)), "final_brief_bytes": len(core.selection.encoded(result["brief"])),
                "replay_trace_not_new_calls": result["trace"], "source_quote_acceptance_is_not_full_answer": True}
            report["cases"].append(entry)
        for query in scope["controls"]:
            result = pipe.run(query, as_of=scope["reference_at"], planner=lambda _: (_ for _ in ()).throw(ValueError("guard_failed")))
            original = pipe.bridge.base.gateway.recall({"query": query, "as_of": scope["reference_at"], "limit": 10})
            report["controls"].append({"model_calls": result["trace"]["model_calls"], "original_unchanged": result["audit"]["query_shadow"]["original"] == original})
    report["actual_api_calls"] = sum(read(directory / (s + ".report.json"))["new_model_calls"] for s in CEILINGS)
    report["reserved_usd"] = str(sum((Decimal(read(directory / (s + ".report.json"))["reserved_usd"]) for s in CEILINGS), Decimal(0)))
    checked(directory)
    write(directory / "evaluation.private.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "prepare-shortlist", "prepare-review", "plan-model", "shortlist-model", "review-model", "evaluate"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(args.scope, args.output)
    elif args.action.startswith("prepare-"):
        result = prepare_next(args.output, args.model, args.action.removeprefix("prepare-"))
    elif args.action == "evaluate":
        result = evaluate(args.output, args.model)
    else:
        result = run_phase(args.output, args.action.removesuffix("-model"), args.key_file, args.ledger)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
