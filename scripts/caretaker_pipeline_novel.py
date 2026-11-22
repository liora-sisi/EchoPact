"""New fixed fictional questions, frozen before first live use; no transport.

Expected labels are evaluator-only. Preparing an offline oracle is a wiring
check and never counts as a live-model or independent factual accuracy result.
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.memory.identity import register_agent
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package
from scripts.caretaker_pipeline_challenge import reference_draft, score as previous_score

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/caretaker_pipeline_novel.json"
AS_OF = "2034-05-02T00:00:00Z"
OWNER = "novel-synthetic-owner"
OUTSIDER = "novel-synthetic-outsider"


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def create_corpus(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    records = []
    for case in load_cases():
        for row in case["records"]:
            records.append({"record_id": row["id"], "source_kind": "synthetic_conversation",
                "source_ref": "synthetic://novel-pipeline-exam", "conversation_id": case["case_id"],
                "message_id": row["id"], "role": row["role"], "content": row["content"],
                "created_at": "2034-05-01T00:00:00Z", "verified": False,
                "authority": "synthetic-unverified", "source_cutoff_at": "2034-05-01T00:00:00Z",
                "branch_memberships": [{"branch_id": row.get("branch", "main"), "position": row["position"]}]})
    package, db = directory / "synthetic-source.json", directory / "synthetic.sqlite3"
    with package.open("x", encoding="utf-8") as stream:
        json.dump({"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records}, stream, ensure_ascii=False)
    for agent in (OWNER, OUTSIDER):
        register_agent(agent, "Synthetic novel questions", actor="synthetic-novel", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id=OWNER, actor="synthetic-novel")
    return db


def score(case, result):
    report = previous_score(case, result)
    draft = result["audit"].get("selection", {}).get("judgments", {})
    if "expected_gaps" in case:
        report["checks"]["independent_gaps"] = draft.get("gaps") == case["expected_gaps"]
    report["passed"] = all(report["checks"].values())
    return report
