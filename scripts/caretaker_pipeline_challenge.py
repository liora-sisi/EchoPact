"""Fixed fictional challenge cases; offline harness, never a provider client."""
from __future__ import annotations

import json
from pathlib import Path

from backend.memory.identity import register_agent
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package
from scripts.caretaker_pipeline_exam import score as base_score

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/caretaker_pipeline_challenge.json"
AS_OF = "2033-04-06T00:00:00Z"
OWNER = "challenge-synthetic-owner"
OUTSIDER = "challenge-synthetic-outsider"


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def create_corpus(directory):
    """Build this fixed fixture only, without importing its gold labels."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    records = []
    for case in load_cases():
        for row in case["records"]:
            records.append({"record_id": row["id"], "source_kind": "synthetic_conversation",
                "source_ref": "synthetic://pipeline-challenge", "conversation_id": case["case_id"],
                "message_id": row["id"], "role": row["role"], "content": row["content"],
                "created_at": "2033-04-05T00:00:00Z", "verified": False,
                "authority": "synthetic-unverified", "source_cutoff_at": "2033-04-05T00:00:00Z",
                "branch_memberships": [{"branch_id": "main", "position": row["position"]}]})
    package, db = directory / "synthetic-source.json", directory / "synthetic.sqlite3"
    with package.open("x", encoding="utf-8") as stream:
        json.dump({"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records}, stream, ensure_ascii=False)
    for agent in (OWNER, OUTSIDER):
        register_agent(agent, "Synthetic challenge", actor="synthetic-challenge", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id=OWNER, actor="synthetic-challenge")
    return db


def reference_draft(case, request):
    """Gold-aware offline wiring check. It is NOT a model quality result."""
    by_text = {r["content"]: r for r in case["records"]}
    rows = []
    for candidate in request["candidates"]:
        row = by_text.get(candidate["text"])
        selected = bool(row and row["selected"])
        rows.append({"candidate_id": candidate["candidate_id"],
            "relation": row["relation"] if selected else "context",
            "quote": candidate["text"] if selected else ""})
    return {"judgments": rows, "sufficiency": case["expected_sufficiency"], "gap": case["expected_gap"]}


def score(case, result):
    report = base_score(case, result)
    stage = result["audit"].get("selection", {})
    items = result["brief"].get("items", [])
    prepared = stage.get("prepared", {})
    cards = prepared.get("request", {}).get("candidates", [])
    report["checks"]["review_has_challenge"] = len(cards) >= case["minimum_review_candidates"]
    # A correct quote with the wrong certainty is still a substantive failure.
    report["checks"]["target_relations"] = all(any(
        item.get("relation") == row["relation"] and all(q in item["text"] for q in row["required"])
        and any(ref["record_id"] == row["id"] for ref in item.get("references", []))
        for item in items) for row in case["records"] if row["selected"])
    report["checks"]["coverage_not_overruled"] = result["brief"].get("needs_more_evidence") is True
    report["passed"] = all(report["checks"].values())
    report["review_candidate_count"] = len(cards)
    return report
