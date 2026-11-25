#!/usr/bin/env python3
"""Replay saved round-two synthetic long answers locally; never call an API."""
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
from backend.memory.summary_reading_card import (
    compact, expand_preview, fingerprint, make_audit, render_preview,
)
from scripts import prepare_summary_round2 as exam
from scripts import run_summary_round2 as previous
from scripts import run_summary_comprehensive_comparison as shared

CASE_IDS = ("long-daily-01", "long-emotion-01", "long-intimacy-01")


def replay(bundle, finals, *, max_bytes=4096):
    """Select only public synthetic long cases. Failed drafts remain diagnostics."""
    public = {c["case_id"]: c for c in exam.load_cases() if c["case_id"] in CASE_IDS}
    cases = {c["case_id"]: c for c in bundle["reviewer_sheet.json"]["cases"]}
    if set(finals) != set(previous.PROVIDERS):
        raise SummaryError("reading_replay_requires_both_saved_providers")
    for cid in CASE_IDS:
        if cid not in cases or cases[cid]["records"] != public[cid]["records"]:
            raise SummaryError("reading_replay_requires_exact_public_sources")
    artifacts, rows = {}, []
    for provider in previous.PROVIDERS:
        final = finals[provider]
        if final.get("model") != shared.PROVIDERS[provider]["model"]:
            raise SummaryError("reading_replay_saved_model_mismatch")
        results = {r["case_id"]: r for r in previous.inspect_final(final, bundle)}
        for cid in CASE_IDS:
            case, outcome = cases[cid], results[cid]
            if outcome["summary"] is None:
                raise SummaryError("reading_replay_saved_answer_unavailable")
            audit = make_audit(outcome["summary"], records=case["records"],
                catalog=case["evidence_catalog"],
                identity={"source_namespace": "synthetic", "room_id": cid, "branch_id": "fixture"},
                author={"provider": provider, "model": final["model"], "recipe": exam.RECIPE,
                        "prompt_sha256": bundle["manifest.json"]["prompt_sha256"]},
                structural_errors=outcome["structural_errors"])
            card = render_preview(audit, max_bytes=max_bytes)
            expansions = [expand_preview(audit, card_id=card["card_id"], fact_id=f"f{i+1}",
                current_records=case["records"], identity=audit["identity"])
                for i in range(len(outcome["summary"]["facts"]))]
            size = len(compact(card))
            original_size = len(compact(outcome["summary"]))
            source_size = sum(len(r["content"].encode("utf-8")) for r in case["records"])
            delivered = card["status"] == "diagnostic_preview"
            rows.append({"provider": provider, "case_id": cid,
                "source_chars": sum(len(r["content"]) for r in case["records"]),
                "source_text_bytes": source_size, "old_author_json_bytes": original_size,
                "backend_audit_bytes": len(compact(audit)), "reading_card_bytes": size,
                "card_delivered": delivered, "structural_errors": outcome["structural_errors"],
                "draft_status": outcome["summary"]["status"], "production_accepted": False,
                "reading_to_source_ratio": round(size / source_size, 6) if delivered else None,
                "reading_to_old_json_ratio": round(size / original_size, 6) if delivered else None,
                "card_plus_first_expansion_bytes": size + len(compact(expansions[0]))
                    if delivered and expansions else None,
                "card_plus_all_expansions_bytes": size + sum(len(compact(e)) for e in expansions)
                    if delivered else None,
                "fact_count": len(outcome["summary"]["facts"]),
                "history_count": sum(f["supersedes"] is not None for f in outcome["summary"]["facts"]),
                "uncertainty_count": len(outcome["summary"]["uncertainties"]),
                "card_id": card["card_id"]})
            artifacts[f"{provider}-{cid}"] = {"audit": audit, "reading_card": card,
                                              "expansions": expansions}
    return {"schema": "echo-summary-reading-replay-v1", "model_calls": 0,
            "production_accepted": False, "includes_failed_drafts_for_diagnosis": True,
            "measurement": "utf8_compact_json_bytes_not_tokens_not_production_mcp_envelope",
            "rows": rows}, artifacts


def markdown(report, artifacts):
    lines = ["# 摘要短卡本地对照", "", "只重放已有合成题答卷，零模型调用。所有短卡均未获准正式使用。",
        "正文、旧说法、未知项保留；后台档案与短卡分开。尺寸为 UTF-8 字节，不是 token。",
        "下表中的旧结构只含作者结果；新短卡已含来源、指针和提示，仍不含生产 MCP 外层。", "",
        "| 模型 | 题目 | 原文 | 旧结构 | 短卡 | 展开第一项后累计 |",
        "|---|---|---:|---:|---:|---:|"]
    for row in report["rows"]:
        lines.append(f"| {row['provider']} | {row['case_id']} | {row['source_text_bytes']} | "
                     f"{row['old_author_json_bytes']} | {row['reading_card_bytes']} | "
                     f"{row['card_plus_first_expansion_bytes']} |")
    lines += ["", "后台保留原答卷、原始片段、精确位置和模型身份；变短不代表内容正确。",
              "已有语义问题见独立的上一轮复核，本次没有替模型改答案或重判为合格。",
              "取全部逐项证据会有重复和额外开销，完整成本已单列在 report.json。"]
    for key, artifact in artifacts.items():
        card = artifact["reading_card"]
        lines += ["", "## " + key, "", "```text", card.get("body", card.get("notice", "")), "```", "",
                  "结构诊断：" + (", ".join(artifact["audit"]["review"]["structural_errors"]) or "无机械错误；不等于语义合格"),
                  "", "原话展开样例："]
        if artifact["expansions"]:
            first = artifact["expansions"][0]
            lines += ["```text"]
            for sid in first["current_refs"]:
                span = first["evidence"][sid]
                lines += [f"{span['record_id']} [{span['start']}:{span['end']}]", span["quote"]]
            lines += ["```"]
    return "\n".join(lines) + "\n"


def run(suite_dir, trial_dir, output_dir, *, max_bytes=4096):
    suite_dir, trial_dir, output_dir = map(shared.protected, (suite_dir, trial_dir, output_dir))
    bundle = previous.load_frozen(suite_dir)
    tracked = [suite_dir / n for n in ("manifest.json", "writer_inputs.json", "reviewer_sheet.json")]
    finals = {}
    for provider in previous.PROVIDERS:
        request_path = trial_dir / provider / "request.json"
        final_path = trial_dir / provider / "final-response.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if request != previous.payload_for(bundle, provider):
            raise SummaryError("reading_replay_saved_request_mismatch")
        finals[provider] = json.loads(final_path.read_text(encoding="utf-8"))
        tracked.extend([request_path, final_path])
    review_path = trial_dir / "semantic-review-v1.private.json"
    old_review = json.loads(review_path.read_text(encoding="utf-8"))
    tracked.append(review_path)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked}
    report, artifacts = replay(bundle, finals, max_bytes=max_bytes)
    for row in report["rows"]:
        found = [r for r in old_review["per_case"]
                 if r["case_id"] == row["case_id"] and r["provider"] == row["provider"]]
        if len(found) != 1 or found[0].get("production_accepted") is not False:
            raise SummaryError("reading_replay_previous_review_required")
        artifacts[f"{row['provider']}-{row['case_id']}"]["previous_review"] = found[0]
    report["input_file_sha256"] = before
    report["renderer_source_sha256"] = hashlib.sha256(
        (ROOT / "backend/memory/summary_reading_card.py").read_bytes()).hexdigest()
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != before[str(p)] for p in tracked):
        raise SummaryError("reading_replay_inputs_changed")
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, artifact in artifacts.items():
        shared.write_new(output_dir / (name + ".json"), artifact)
    with (output_dir / "comparison.md").open("x", encoding="utf-8") as stream:
        stream.write(markdown(report, artifacts))
    shared.write_new(output_dir / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", required=True, type=Path)
    parser.add_argument("--trial-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-bytes", type=int, default=4096)
    args = parser.parse_args(argv)
    try:
        report = run(args.suite_dir, args.trial_dir, args.output_dir, max_bytes=args.max_bytes)
        print(json.dumps({"model_calls": 0, "production_accepted": False, "rows": report["rows"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "error", "code": "reading_card_replay_failed_no_api"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
