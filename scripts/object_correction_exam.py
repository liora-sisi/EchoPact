"""Fixed synthetic sources through the real scout; labels never enter requests."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from backend.memory import object_recall_shadow as scout
from backend.memory import object_evidence_review as review

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/object_correction_synthetic.json"
CONTRAST_FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/object_correction_contrasts.json"
AS_OF = "2031-05-07T00:00:00Z"


def load_cases(partition, *, contrast=False):
    cases = json.loads((CONTRAST_FIXTURE if contrast else FIXTURE).read_text(encoding="utf-8"))
    return [c for c in cases if partition == "all" or c["partition"] == partition]


def build_scout(case):
    rows = [{"record_id": r["id"], "content": r["content"], "role": r.get("role", "assistant"),
             "source_kind": "synthetic", "conversation_id": "synthetic-room",
             "created_at": "2031-05-06T00:00:00Z", "conflict_group_id": None,
             "branch_memberships": [{"branch_id": r.get("branch", "main"), "position": r["position"]}]}
            for r in case["records"]]
    def runner(args):
        matches = []
        for row in rows:
            if args["query"] not in row["content"]:
                continue
            primary = copy.deepcopy(row)
            p = primary["branch_memberships"][0]
            primary["conversation_context"] = []
            for child in rows:
                c = child["branch_memberships"][0]
                relative = c["position"] - p["position"]
                if c["branch_id"] == p["branch_id"] and -1 <= relative <= 2 and relative != 0:
                    context = copy.deepcopy(child)
                    context["branch_memberships"][0]["relative_position"] = relative
                    primary["conversation_context"].append(context)
            matches.append(primary)
        return {"memories": matches[:args["limit"]], "coverage": {"coverage_gap": False}}
    shadow = {"plan": {"status": "planned", "intent": "comparison", "topic_terms": [case["subject"]]}}
    return scout.run_object_shadow(shadow, query=case["query"], as_of=AS_OF, runner=runner)


def prepare_case(case):
    return review.prepare(build_scout(case))
