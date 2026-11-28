#!/usr/bin/env python3
"""Compare card/event delivery over frozen synthetic answers without a model call."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory.caretaker_summary import SummaryError
from backend.memory import summary_event_booklet as books
from backend.memory import summary_reading_card as reading
from scripts import summary_event_review_trial as review_trial

RECIPE = "echo-summary-event-delivery-comparison-v1"
DELIVERY_LIMIT = 1_000_000


def compare(audit, booklet, records):
    """All single-event selections are explicit probes, not query-retrieval grades."""
    identity = audit["source"]
    common = {"current_records": records, "identity": identity, "max_bytes": DELIVERY_LIMIT}
    catalogue = books.catalogue(audit, booklet, **common)
    if not booklet["layout_complete"] or catalogue["status"] != "unapproved_preview":
        raise SummaryError("delivery_comparison_requires_complete_layout")
    event_ids = [e["event_id"] for e in audit["draft"]["events"]]
    if not event_ids:
        raise SummaryError("delivery_comparison_requires_events")
    original_events = {e["event_id"]: e for card in booklet["cards"] for e in card["events"]}
    rows, packets = [], []
    # An explicit all-events measurement prevents claiming every request shrinks.
    selections = [("single:" + eid, [eid]) for eid in event_ids] + [("all", event_ids)]
    for label, selected in selections:
        card_ids = list(dict.fromkeys(booklet["event_to_card"][eid] for eid in selected))
        legacy = books.read_cards(audit, booklet, card_ids=card_ids, **common)
        current = books.read_events(audit, booklet, event_ids=selected, **common)
        if legacy["status"] != "unapproved_preview" or current["status"] != "unapproved_preview":
            raise SummaryError("delivery_comparison_packet_withheld")
        if (current["events"] != [original_events[eid] for eid in current["included_event_ids"]]
                or current["declared_gaps"] != legacy["declared_gaps"]
                or current["global_uncertainties"] != legacy["global_uncertainties"]):
            raise SummaryError("delivery_comparison_changed_content")
        old_size, new_size = len(reading.compact(legacy)), len(reading.compact(current))
        rows.append({"selection": label, "requested_event_ids": selected,
            "legacy_included_event_ids": [e["event_id"] for c in legacy["cards"] for e in c["events"]],
            "included_event_ids": current["included_event_ids"],
            "legacy_read_bytes": old_size, "event_read_bytes": new_size,
            "saved_bytes": old_size - new_size, "saved_percent": round(100 * (old_size - new_size) / old_size, 2),
            "catalogue_plus_legacy_read_bytes": len(reading.compact(catalogue)) + old_size,
            "catalogue_plus_event_read_bytes": len(reading.compact(catalogue)) + new_size})
        packets.append({"selection": label, "legacy": legacy, "event": current})
    # All event/fact/unknown IDs and expansion context retain the existing interface.
    pointers = [f"{e['event_id']}.f{i}" for e in audit["draft"]["events"] for i in range(1, len(e["facts"]) + 1)]
    pointers += [f"{e['event_id']}.u{i}" for e in audit["draft"]["events"] for i in range(1, len(e["uncertainties"]) + 1)]
    pointers += [f"u{i}" for i in range(1, len(audit["draft"]["uncertainties"]) + 1)]
    pointers += [f"r{i}" for i in range(1, len(audit["draft"]["relations"]) + 1)]
    for pointer in pointers:
        expanded = books.expand(audit, booklet, item_id=pointer, **common)
        if expanded["status"] != "unapproved_preview":
            raise SummaryError("delivery_comparison_expansion_withheld")
        for span in expanded["evidence"].values():
            record = next(r for r in records if r["record_id"] == span["record_id"])
            if record["content"][span["start"]:span["end"]] != span["quote"]:
                raise SummaryError("delivery_comparison_changed_quote")
    return {"source_window_sha256": audit["source_window_sha256"],
        "audit_sha256": reading.fingerprint(audit), "book_id": booklet["book_id"],
        "events": len(event_ids), "exact_expansions_checked": len(pointers),
        "original_body_bytes": sum(len(r["content"].encode("utf-8")) for r in records),
        "catalogue_bytes": len(reading.compact(catalogue)), "selections": rows,
        "semantic_passed": None, "production_accepted": False}, packets


def replay(suite_directory, first_directory, prior_directory, output_directory):
    paths = [review_trial.transport.protected(p) for p in
             (suite_directory, first_directory, prior_directory, output_directory)]
    suite_directory, first_directory, prior_directory, output_directory = paths
    suite = review_trial.load_frozen(suite_directory, first_directory, review_version="v3")
    observed = {}
    def read(path):
        blob = path.read_bytes()
        observed[path] = hashlib.sha256(blob).hexdigest()
        return json.loads(blob)
    if read(prior_directory / "manifest.json") != suite["manifest.json"]:
        raise SummaryError("delivery_comparison_changed_run")
    report = {"schema": RECIPE, "model_calls": 0, "source_kind": "synthetic_only",
        "evaluation": "explicit_event_selection_delivery_not_retrieval_or_semantic_acceptance",
        "production_accepted": False, "cases": []}
    artifacts = {}
    for job, case in zip(suite["jobs.json"], suite["reviewer.json"]):
        folder = prior_directory / job["job_id"]
        if (read(folder / "request.json") != job["request"]
                or read(folder / "attempt.json") != {k: v for k, v in job.items() if k != "request"}):
            raise SummaryError("delivery_comparison_changed_request")
        final = read(folder / "final-response.json")
        inspected = review_trial.inspect_final(final, job, case)
        for name in ("audit", "booklet", "candidate-raw"):
            if read(folder / (name + ".json")) != inspected[name]:
                raise SummaryError("delivery_comparison_changed_artifact")
        measured, packets = compare(inspected["audit"], inspected["booklet"], case["records"])
        measured.update(case_id=case["case_id"], first_review_response_sha256=observed[folder / "final-response.json"])
        report["cases"].append(measured)
        artifacts[case["case_id"]] = packets
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != sha for p, sha in observed.items()):
        raise SummaryError("delivery_comparison_input_changed_during_replay")
    output_directory.mkdir(parents=True, exist_ok=False)
    for name, data in {"report": report, **artifacts}.items():
        review_trial.transport.write_new(output_directory / (name + ".json"), data)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("suite-dir", "first-answer-dir", "prior-run-dir", "output-dir"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = replay(args.suite_dir, args.first_answer_dir, args.prior_run_dir, args.output_dir)
        print(json.dumps({"status": "offline_comparison_saved", "model_calls": 0,
            "cases": len(report["cases"]), "production_accepted": False}))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "error", "code": "delivery_comparison_failed_details_suppressed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
