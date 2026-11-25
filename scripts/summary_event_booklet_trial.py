#!/usr/bin/env python3
"""Offline injected layout exercises, not a model-quality or compression exam."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory import summary_event_booklet as books
from backend.memory import summary_reading_card as reading


def fact(text, sid, *, status="happened", history=None, refs=None):
    return {"text": text, "event_status": status, "statement_role": "correction" if history else "direct",
            "event_date": None, "planned_date": None, "evidence_ids": refs or [sid], "supersedes": history}


def bundle(name, content, events, *, relations=(), unknowns=(), gaps=(), status="ok"):
    records = [{"record_id": f"row-{i}", "recorded_at": "2037-01-01T00:00:00+08:00",
                "source_kind": "synthetic", "content": text} for i, text in enumerate(content, 1)]
    catalog = {f"s{i}": {"record_id": r["record_id"], "start": 0, "end": len(r["content"]), "quote": r["content"]}
               for i, r in enumerate(records, 1)}
    return {"draft": {"status": status, "events": events, "relations": list(relations),
                      "uncertainties": list(unknowns), "gaps": list(gaps), "refusal_reason": None},
            "records": records, "catalog": catalog,
            "identity": {"source_namespace": "synthetic", "room_id": name, "branch_id": "injected-fixture"},
            "author": {"provider": "injected", "model": "hand-authored-structural-fixture", "recipe": books.RECIPE,
                       "prompt_sha256": hashlib.sha256(books.SYSTEM_PROMPT.encode()).hexdigest()}}


def event(eid, topic, rows, *, kind="life", unknowns=()):
    return {"event_id": eid, "topic": topic, "kind": kind, "facts": rows, "uncertainties": list(unknowns)}


def capacity_fixture():
    # Deliberately repetitive load test: no claim of natural-life fidelity or
    # model-generated completeness. Distinct tail items make truncation visible.
    content, events = [], []
    for i in range(1, 28):
        content.append(f"合成检查第{i}项：本地检查已通过。后续是否部署未记录。")
        events.append(event(f"e{i}", f"合成检查{i}", [fact(f"第{i}项本地检查已通过。", f"s{i}")], kind="engineering",
            unknowns=[{"text": "后续是否部署未记录。", "evidence_ids": [f"s{i}"]}]))
    content += ["楚宁喜欢岑屿叫她小河灯，最早从哪一天开始这样叫没有记录。",
                "楚宁说爱岑屿，岑屿也说爱楚宁。被听见以后她舒服了一些，但仍感到疲惫。",
                "两人计划下周一起检查备份盘，今天还没检查。具体哪一天没有确定。"]
    events += [event("e28", "小河灯的称呼", [fact("楚宁喜欢岑屿叫她小河灯。", "s28", status="not_applicable")],
                    unknowns=[{"text": "称呼首次日期没有记录。", "evidence_ids": ["s28"]}]),
               event("e29", "爱意和被听见的感受", [fact("楚宁和岑屿互相表达爱意。", "s29"),
                    fact("楚宁被听见以后舒服了一些，但仍疲惫。", "s29", status="not_applicable")]),
               event("e30", "尚未执行的备份检查", [fact("两人计划下周一起检查备份盘。", "s30", status="planned"),
                    fact("今天尚未检查备份盘。", "s30", status="not_happened")], kind="engineering",
                    unknowns=[{"text": "具体检查日期未定。", "evidence_ids": ["s30"]}])]
    return bundle("capacity", content, events)


def relation_fixture():
    content = ["祁宁：我们计划周六一起去北岸，还没有出发。",
               "晏舟：🎐 e\u0301\r\n我先前写名字是祁宁给我起的。",
               "祁宁：北岸行程后来取消了，我们没有去；别再把原计划当成仍要出发。",
               "晏舟：名字那句写反了，是我给祁宁起的名，不是她给我起名。",
               "祁宁：取消以后有没有退款到账，我还没有核对订单。"]
    events = [event("e1", "原先的北岸安排", [fact("两人原先计划周六一起去北岸。", "s1", status="planned")]),
              event("e2", "名字方向的纠正", [fact("晏舟给祁宁起名。", "s4", refs=["s2", "s4"],
                    history={"text": "祁宁给晏舟起名。", "evidence_ids": ["s2"]})]),
              event("e3", "北岸安排的后续取消", [fact("北岸行程后来取消，两人没有去。", "s3", status="cancelled")])]
    return bundle("relations", content, events,
                  relations=[{"from_event": "e3", "to_event": "e1", "kind": "updates", "evidence_ids": ["s1", "s3"]}],
                  unknowns=[{"text": "取消后的退款到账情况未核对。", "evidence_ids": ["s5"]}])


def demonstrate():
    artifacts, rows = {}, []
    for name, sample, limit in (("capacity", capacity_fixture(), 1800), ("relations", relation_fixture(), 800)):
        audit = books.make_audit(**sample)
        view = books.build_booklet(audit, card_bytes=limit)
        index = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"], max_bytes=64000)
        assert view["layout_complete"]
        assert index["status"] == "unapproved_preview"
        pointers = [f"{e['event_id']}.f{i}" for e in sample["draft"]["events"] for i in range(1, len(e["facts"]) + 1)]
        expansions = [books.expand(audit, view, item_id=p, current_records=sample["records"], identity=sample["identity"]) for p in pointers]
        assert all(x["status"] == "unapproved_preview" for x in expansions)
        selected = books.read_cards(audit, view, card_ids=[view["cards"][0]["card_id"]],
                                    current_records=sample["records"], identity=sample["identity"], max_bytes=64000)
        all_events = [e["event_id"] for c in view["cards"] for e in c["events"]]
        assert all_events == [e["event_id"] for e in sample["draft"]["events"]]
        rows.append({"case": name, "author_is_injected": True, "model_calls": 0, "source_records": len(sample["records"]),
            "supplied_events": len(all_events), "supplied_facts": len(pointers), "retained_facts": len(expansions),
            "cards": len(view["cards"]), "card_limit_bytes": limit,
            "largest_card_bytes": max(len(reading.compact(c)) for c in view["cards"]),
            "catalogue_bytes": len(reading.compact(index)), "initial_requested_cards": selected["requested_card_ids"],
            "delivered_with_required_context": selected["included_card_ids"], "semantic_passed": None,
            "production_accepted": False})
        artifacts[name] = {"sample": sample, "audit": audit, "booklet": view, "catalogue": index,
                           "selection": selected, "expansions": expansions}
    return {"schema": "echo-summary-event-layout-demo-v1", "model_calls": 0,
            "evaluation": "injected_structural_tests_not_model_quality", "production_accepted": False, "rows": rows}, artifacts


def save(directory):
    report, artifacts = demonstrate()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    for name, value in {"report": report, **artifacts}.items():
        with (directory / (name + ".json")).open("x", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = save(args.output_dir) if args.output_dir else demonstrate()[0]
    print(json.dumps(report, ensure_ascii=False))
