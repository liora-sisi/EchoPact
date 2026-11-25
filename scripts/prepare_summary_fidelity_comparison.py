#!/usr/bin/env python3
"""Prepare one synthetic authoring exam; no credentials, network or model calls."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory.caretaker_summary import SummaryError, validate_records
from backend.memory import caretaker_summary_v4 as contract


FIXTURE = ROOT / "tests/fixtures/summary_fidelity_synthetic.json"
CANDIDATES = (
    {"provider": "deepseek", "model": "deepseek-flash"},
    {"provider": "venice", "model": "mistral-small-3-2-24b-instruct"},
    {"provider": "openai", "model": "gpt-5.6-sol"},
)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def load_cases():
    # Deliberately no arbitrary input path or real-archive option.
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def build_bundle(cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 12:
        raise SummaryError("invalid_fidelity_case_count")
    inputs, reviews, seen = [], [], set()
    for case in cases:
        if (not isinstance(case, dict)
                or set(case) != {"case_id", "focus", "records", "review_targets", "forbidden_claims"}):
            raise SummaryError("invalid_fidelity_case_fields")
        cid = case["case_id"]
        if not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9_-]{1,80}", cid) or cid in seen:
            raise SummaryError("invalid_fidelity_case_id")
        seen.add(cid)
        if not isinstance(case["focus"], str) or not case["focus"].strip():
            raise SummaryError("invalid_fidelity_focus")
        for key in ("review_targets", "forbidden_claims"):
            values = case[key]
            if (not isinstance(values, list) or not 1 <= len(values) <= 8
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in values)):
                raise SummaryError("invalid_fidelity_review_items")
        validate_records(case["records"])
        inputs.append({"case_id": cid, "messages": contract.build_messages(case["records"]),
                       "response_schema": copy.deepcopy(contract.SUMMARY_SCHEMA)})
        reviews.append({**copy.deepcopy(case), "evidence_catalog": contract.evidence_catalog(case["records"]),
                        "grades": [{**candidate, "status": "not_run", "semantic_passed": None,
                                    "reviewer": None, "notes": None} for candidate in CANDIDATES]})
    writer = {"schema": "echo-summary-fidelity-inputs-v1", "cases": inputs}
    reviewer = {"schema": "echo-summary-fidelity-review-v1", "never_send_to_writer": True,
                "cases": reviews}
    manifest = {
        "schema": "echo-summary-fidelity-preparation-v1", "status": "prepared_not_run",
        "synthetic_only": True, "evaluation_kind": "development_not_blind",
        "contract": "echo-pact-summary-trial-v4", "model_calls": 0,
        "production_accepted": False, "live_runner_implemented": False,
        "candidates": [dict(c, account_access_checked=False) for c in CANDIDATES],
        "case_count": len(cases), "planned_comparison_cells": len(cases) * len(CANDIDATES),
        "record_count": sum(len(c["records"]) for c in cases),
        "source_chars": sum(len(r["content"]) for c in cases for r in c["records"]),
        "fixture_sha256": digest(cases),
        "prompt_sha256": hashlib.sha256(contract.SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "response_schema_sha256": digest(contract.SUMMARY_SCHEMA),
        "files_sha256": {"writer_inputs.json": digest(writer), "reviewer_sheet.json": digest(reviewer)},
        "notes": ["provider_neutral_inputs_not_wire_requests", "review_targets_not_in_messages",
                  "no_new_nameplate_schema_or_omission_markers_implemented", "no_model_quality_score_yet"],
    }
    return {"manifest.json": manifest, "writer_inputs.json": writer, "reviewer_sheet.json": reviewer}


def prepare(output_dir, bundle):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    # Write the manifest last. A partial preparation is never reported as complete.
    for name in ("writer_inputs.json", "reviewer_sheet.json", "manifest.json"):
        with (output_dir / name).open("xb") as stream:
            stream.write(encoded(bundle[name]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.prepare != (args.output_dir is not None):
        parser.error("--prepare and --output-dir must be supplied together")
    try:
        bundle = build_bundle(load_cases())
        if args.prepare:
            prepare(args.output_dir, bundle)
    except FileExistsError:
        print(json.dumps({"status": "error", "code": "output_exists_not_overwritten"}))
        return 2
    except (SummaryError, OSError, ValueError):
        print(json.dumps({"status": "error", "code": "fidelity_preparation_failed"}))
        return 2
    manifest = bundle["manifest.json"]
    result = {k: manifest[k] for k in ("case_count", "record_count", "source_chars",
              "planned_comparison_cells", "model_calls", "production_accepted")}
    result["status"] = "prepared_not_run" if args.prepare else "dry_run"
    result["writer_inputs_sha256"] = manifest["files_sha256"]["writer_inputs.json"]
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
