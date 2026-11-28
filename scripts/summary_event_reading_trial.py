#!/usr/bin/env python3
"""Record a direct, synthetic reading walk: catalogue -> events -> source excerpts.

The reader chooses every step. No question planner, model call, relevance score or
semantic auto-grader is introduced. Known development material is not a blind exam.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory import summary_event_booklet as books
from backend.memory import summary_reading_card as reading
from backend.memory.caretaker_summary import SummaryError
from scripts import summary_event_delivery_trial as delivery

SCHEMA = "echo-summary-direct-reading-trial-v1"
QUESTIONS = [
    {"query_id": "gift", "case_id": "long-dense-01",
     "question": "那条围在脖子上的礼物叫什么？是什么时候收到的？能确定具体哪天吗？"},
    {"query_id": "exhibition", "case_id": "long-dense-01",
     "question": "之前说去看展，后来怎么没去成？是哪天取消的，原来又准备哪天去？"},
    {"query_id": "walk-dinner", "case_id": "long-dense-01",
     "question": "河边那次散步最后更正成哪天了？吃晚饭也是那一天吗？"},
    {"query_id": "trip-refund", "case_id": "long-sparse-01",
     "question": "之前商量的那趟旅行，后来到底去没去？钱退回来了吗，现在还有重新约好的行程吗？"},
    {"query_id": "first-sea", "case_id": "long-dense-01",
     "question": "我们第一次一起去看海是哪一天？"},
]


def size(value):
    return len(reading.compact(value))


def source_packet(case):
    return {"schema": "echo-summary-trial-original-window-v1", "source": case["audit"]["source"],
        "source_window_sha256": case["audit"]["source_window_sha256"],
        "records": case["records"], "production_accepted": False,
        "notice": "仅这份固定合成窗口；未提及不证明完整档案不存在，也不能证明绝对第一次。"}


def visible_items(case, packet):
    if packet.get("status") != "unapproved_preview":
        return set()
    ids = {u["item_id"] for u in packet.get("global_uncertainties", [])}
    ids.update(r["relation_id"] for r in packet.get("relations", []))
    selected = set(packet.get("included_event_ids", []))
    for event in case["audit"]["draft"]["events"]:
        if event["event_id"] in selected:
            for suffix, field in (("f", "facts"), ("u", "uncertainties")):
                ids.update(f"{event['event_id']}.{suffix}{i}" for i in range(1, len(event[field]) + 1))
    return ids


def step(case, prior, request, *, total_limit=16384):
    """Replayable step; full response bodies, including failures/repeats, are counted."""
    books._current(case["audit"], case["booklet"], case["records"], case["audit"]["source"])
    if type(total_limit) is not int or not 1024 <= total_limit <= 128000:
        raise SummaryError("reading_trial_invalid_total_limit")
    common = {"current_records": case["records"], "identity": case["audit"]["source"], "max_bytes": 1_000_000}
    audit, view = case["audit"], case["booklet"]
    seen = set().union(*(visible_items(case, row["response"]) for row in prior))
    operation = request.get("operation")
    if operation == "catalogue" and set(request) == {"operation"}:
        packet = books.catalogue(audit, view, **common)
        old = packet
    elif operation == "events" and set(request) == {"operation", "event_ids"}:
        known = {e["event_id"] for row in prior if row["request"]["operation"] == "catalogue"
                 for e in row["response"].get("events", [])}
        ids = request["event_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(e, str) or e not in known for e in ids):
            raise SummaryError("reading_trial_events_not_seen_in_catalogue")
        packet = books.read_events(audit, view, event_ids=ids, **common)
        card_ids = list(dict.fromkeys(view["event_to_card"][eid] for eid in ids if eid in view["event_to_card"]))
        old = books.read_cards(audit, view, card_ids=card_ids, **common) if card_ids else packet
    elif operation == "expand" and set(request) == {"operation", "item_id"}:
        if request["item_id"] not in seen:
            raise SummaryError("reading_trial_item_not_read")
        packet = books.expand(audit, view, item_id=request["item_id"], **common)
        old = packet
    elif operation == "originals" and set(request) == {"operation"}:
        packet = source_packet(case)
        old = packet
    elif operation == "answer" and set(request) == {"operation", "text", "evidence_ids"}:
        refs = request["evidence_ids"]
        expanded = {row["response"].get("item_id") for row in prior
                    if row["request"]["operation"] == "expand"
                    and row["response"].get("status") == "unapproved_preview"}
        for row in prior:
            if row["response"].get("schema") == "echo-summary-trial-original-window-v1":
                expanded.update("record:" + r["record_id"] for r in row["response"].get("records", []))
        if (not isinstance(request["text"], str) or not request["text"].strip()
                or not isinstance(refs, list) or not refs
                or any(not isinstance(ref, str) or ref not in expanded for ref in refs)):
            raise SummaryError("reading_trial_answer_needs_read_original_evidence")
        packet = {"status": "answer_recorded_not_semantically_approved", "answer_number":
                  1 + sum(r["request"]["operation"] == "answer" for r in prior)}
        old = packet
    else:
        raise SummaryError("reading_trial_invalid_operation")
    required = size(packet)
    spent = sum(row["response_bytes"] for row in prior)
    if spent + required > total_limit:
        packet = {"status": "session_budget_exceeded", "required_bytes": required,
                  "remaining_bytes": total_limit - spent, "notice": "整包暂扣；本次没有读到所请求的材料。"}
        if spent + size(packet) > total_limit:
            raise SummaryError("reading_trial_no_room_for_budget_receipt")
    return {"request": request, "response": packet, "response_bytes": size(packet),
        "counterfactual_card_response_bytes": size(old),
        "counterfactual_card_status": old.get("status", "originals"),
        "cumulative_response_bytes": spent + size(packet)}


def create(directory, inputs, *, questions=QUESTIONS, total_limit=16384):
    cases = delivery.load_cases(**inputs)
    known = {c["case_id"] for c in cases}
    if (not questions or len({q["query_id"] for q in questions}) != len(questions)
            or any(not re.fullmatch(r"[a-z0-9-]{1,60}", q["query_id"])
                   or q["case_id"] not in known or not q["question"].strip() for q in questions)):
        raise SummaryError("reading_trial_invalid_questions")
    if type(total_limit) is not int or not 1024 <= total_limit <= 128000:
        raise SummaryError("reading_trial_invalid_total_limit")
    manifest = {"schema": SCHEMA, "inputs": {k: str(Path(v).resolve()) for k, v in inputs.items()},
        "case_fingerprints": {c["case_id"]: reading.fingerprint(c) for c in cases},
        "questions": questions, "total_limit_per_question": total_limit,
        "evaluation": "known_synthetic_development_walk_in_already_located_window_not_blind_retrieval",
        "model_calls": 0, "semantic_passed": None, "production_accepted": False}
    directory = delivery.review_trial.transport.protected(directory)
    directory.mkdir(parents=True, exist_ok=False)
    delivery.review_trial.transport.write_new(directory / "manifest.json", manifest)
    return manifest


def load(directory):
    directory = delivery.review_trial.transport.protected(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema"] != SCHEMA:
        raise SummaryError("reading_trial_wrong_schema")
    cases = {c["case_id"]: c for c in delivery.load_cases(**manifest["inputs"])}
    if {key: reading.fingerprint(c) for key, c in cases.items()} != manifest["case_fingerprints"]:
        raise SummaryError("reading_trial_changed_case")
    questions = {q["query_id"]: q for q in manifest["questions"]}
    rows, previous = [], reading.fingerprint(manifest)
    for index, path in enumerate(sorted(directory.glob("step-*.json")), 1):
        if path.name != f"step-{index:03d}.json":
            raise SummaryError("reading_trial_missing_step")
        row = json.loads(path.read_text(encoding="utf-8"))
        qid = row["query_id"]
        expected = step(cases[questions[qid]["case_id"]], [r for r in rows if r["query_id"] == qid],
                        row["request"], total_limit=manifest["total_limit_per_question"])
        expected.update(query_id=qid, previous_sha256=previous)
        if row != expected:
            raise SummaryError("reading_trial_changed_trace")
        rows.append(row)
        previous = reading.fingerprint(row)
    return manifest, cases, rows, previous


def act(directory, query_id, request):
    manifest, cases, rows, previous = load(directory)
    question = next((q for q in manifest["questions"] if q["query_id"] == query_id), None)
    if question is None:
        raise SummaryError("reading_trial_unknown_question")
    row = step(cases[question["case_id"]], [r for r in rows if r["query_id"] == query_id], request,
               total_limit=manifest["total_limit_per_question"])
    row.update(query_id=query_id, previous_sha256=previous)
    path = delivery.review_trial.transport.protected(directory) / f"step-{len(rows) + 1:03d}.json"
    # Exclusive creation preserves the first step if two writers race.
    with path.open("x", encoding="utf-8") as output:
        output.write(reading.compact(row).decode("utf-8"))
    return row["response"]


def report(directory):
    manifest, cases, rows, previous = load(directory)
    results = []
    for q in manifest["questions"]:
        trace = [r for r in rows if r["query_id"] == q["query_id"]]
        case = cases[q["case_id"]]
        responses = [r["response"] for r in trace]
        answers = [r["request"] for r in trace if r["request"]["operation"] == "answer"
                   and r["response"].get("status") == "answer_recorded_not_semantically_approved"]
        results.append({**q, "steps": len(trace), "answers": answers,
            "first_selected_event_ids": next((r["request"]["event_ids"] for r in trace
                                              if r["request"]["operation"] == "events"), []),
            "event_reads": sum(r["request"]["operation"] == "events" for r in trace),
            "source_expansions": sum(r["request"]["operation"] == "expand" for r in trace),
            "whole_window_reads": sum(r["request"]["operation"] == "originals" for r in trace),
            "response_bytes": sum(r["response_bytes"] for r in trace),
            "request_bytes": sum(size(r["request"]) for r in trace),
            "question_bytes": len(q["question"].encode("utf-8")),
            "same_steps_card_counterfactual_bytes": sum(r["counterfactual_card_response_bytes"] for r in trace),
            "budget_withholds": sum(p.get("status") == "session_budget_exceeded" for p in responses),
            "original_body_only_bytes": sum(len(r["content"].encode("utf-8")) for r in case["records"]),
            "whole_original_window_packet_bytes": size(source_packet(case))})
    return {"schema": SCHEMA, "evaluation": manifest["evaluation"], "trace_sha256": previous,
        "model_calls": 0, "semantic_passed": None, "production_accepted": False, "questions": results,
        "measurement": "canonical_UTF8_response_JSON_including_metadata_repeats_receipts; requests_separate; not_tokens",
        "baseline": "same_selected_steps_with_legacy_cards; original_window_is_size_reference_not_search_baseline"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "step", "report"))
    parser.add_argument("--session-dir", required=True, type=Path)
    for name in ("suite-dir", "first-answer-dir", "prior-run-dir", "request-file"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--query-id")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            result = create(args.session_dir, {"suite_directory": args.suite_dir,
                "first_directory": args.first_answer_dir, "prior_directory": args.prior_run_dir})
            result = {"status": "questions_frozen", "questions": result["questions"]}
        elif args.action == "step":
            result = act(args.session_dir, args.query_id, json.loads(args.request_file.read_text(encoding="utf-8-sig")))
        else:
            result = report(args.session_dir)
        print(reading.compact(result).decode("utf-8"))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print('{"status":"error","code":"reading_trial_failed_details_suppressed"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
