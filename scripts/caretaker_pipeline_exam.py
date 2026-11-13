"""Two fixed fictional integration cases. No model transport or private input."""
from __future__ import annotations

import json
from pathlib import Path

from backend.memory.identity import register_agent
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/caretaker_pipeline_fresh.json"
AS_OF = "2032-03-05T00:00:00Z"
OWNER = "pipeline-synthetic-owner"
OUTSIDER = "pipeline-synthetic-outsider"


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def create_corpus(directory):
    """Create a fresh, explicitly synthetic DB; never open an existing one."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    records = []
    for case in load_cases():
        for row in case["records"]:
            records.append({"record_id": row["id"], "source_kind": "synthetic_conversation",
                "source_ref": "synthetic://pipeline-exam", "conversation_id": case["case_id"],
                "message_id": row["id"], "role": row["role"], "content": row["content"],
                "created_at": "2032-03-04T00:00:00Z", "verified": False,
                "authority": "synthetic-unverified", "source_cutoff_at": "2032-03-04T00:00:00Z",
                "branch_memberships": [{"branch_id": "main", "position": row["position"]}]})
    package, db = directory / "synthetic-source.json", directory / "synthetic.sqlite3"
    with package.open("x", encoding="utf-8") as stream:
        json.dump({"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records}, stream, ensure_ascii=False)
    for agent in (OWNER, OUTSIDER):
        register_agent(agent, "Synthetic integration", actor="synthetic-exam", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id=OWNER, actor="synthetic-exam")
    return db


def reference_draft(case, request):
    """Offline wiring oracle, never passed to a live provider or called a model."""
    selected = {r["content"] for r in case["records"] if r["selected"]}
    return {"judgments": [{"candidate_id": c["candidate_id"],
        "relation": "answers" if c["text"] in selected else "context",
        "quote": c["text"] if c["text"] in selected else ""} for c in request["candidates"]],
        "sufficiency": case["expected_sufficiency"], "gap": case["expected_gap"]}


def score(case, result):
    """Visible source quotes and forbidden selections, not packet length alone."""
    selected = [r for r in case["records"] if r["selected"]]
    forbidden = {r["id"] for r in case["records"] if not r["selected"]}
    items = result["brief"].get("items", [])
    seen = {r["record_id"] for item in items for r in item.get("references", [])}
    target_checks = [{"record_id": row["id"], "present": any(
        any(ref["record_id"] == row["id"] for ref in item.get("references", []))
        and all(q in item["text"] for q in row["required"]) for item in items)} for row in selected]
    draft = result["audit"].get("selection", {}).get("judgments", {})
    checks = {"route": result["route"] == case["expected_route"],
              "source_contract": result["status"] == "selected_requires_review",
              "targets": all(t["present"] for t in target_checks),
              "forbidden_absent": not bool(seen & forbidden),
              "sufficiency": draft.get("sufficiency") == case["expected_sufficiency"],
              "gap": draft.get("gap") == case["expected_gap"]}
    return {"passed": all(checks.values()), "checks": checks, "targets": target_checks}
