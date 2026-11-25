#!/usr/bin/env python3
"""Freeze second-round source-faithfulness comparisons without making API calls."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory.caretaker_summary import SummaryError
from scripts import prepare_summary_comprehensive_comparison as previous
from scripts.prepare_summary_fidelity_comparison import digest

FIXTURE = ROOT / "tests/fixtures/summary_round2_long_synthetic.json"
REGRESSION_IDS = ("daily-01", "daily-02", "emotion-02", "emotion-03", "intimacy-02", "intimacy-03")
CANDIDATES = tuple(c for c in previous.CANDIDATES if c["provider"] in ("openai", "deepseek"))
RECIPE = "echo-summary-round2-v1"
SUMMARY_SCHEMA = copy.deepcopy(previous.SUMMARY_SCHEMA)
# Longer mixed records need room for life, work and emotions without merging unrelated facts.
SUMMARY_SCHEMA["properties"]["facts"]["maxItems"] = 8
SYSTEM_PROMPT = previous.SYSTEM_PROMPT + """
本轮补充整理要求：
1. 先读到本题结尾，再写当前有效的事实。同一计划后来取消，只列最终 cancelled，原定日仍可保留；不要另列一条依旧有效的 planned。被明确推翻的断言只放 supersedes，不再作为普通事实并列。
2. correction 专指推翻先前已经作出的断言。回答原因疑问、拒绝某个猜测时，用 direct 表达澄清，supersedes=null；保留被排除的原因，不伪造原先已确认的结论。
3. 取消、纠正的当前事实必须同时引用确定对象/日期和后续更改的片段；历史引用不能代替当前事实自己的依据。不同动作分别判状态：已完成的准备仍是 happened，相关活动可能尚未发生或已取消。
4. 只给省略片段时，不擅自补出动作对象、代词对应者、生理原因或液体成分；有歧义的部分如实留在 uncertainties。保留原话实际表达的含义，不续写、不用整段复述代替事实整理。
5. 长材料合并重复表述，保留定案、否定、更正、感受和未决事项。原文超过400字时，所有事实正文、supersedes正文及uncertainties合计目标不超过原文60%；不能为了变短丢关键事实。短片段不强求压缩比。
"""


def load_cases():
    original = {c["case_id"]: c for c in previous.load_cases()}
    new = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if len(new) != 3 or any(sum(len(r["content"]) for r in c["records"]) < 400 for c in new):
        raise SummaryError("round2_requires_three_long_cases")
    return [copy.deepcopy(original[cid]) for cid in REGRESSION_IDS] + new


def build_bundle(private_packet=None):
    cases = load_cases()
    bundle = previous.build_bundle(cases, private_packet)
    writer, review, manifest = (bundle[n] for n in ("writer_inputs.json", "reviewer_sheet.json", "manifest.json"))
    for case in writer["cases"]:
        case["messages"][0]["content"] = SYSTEM_PROMPT
        case["response_schema"] = copy.deepcopy(SUMMARY_SCHEMA)
    for case in review["cases"]:
        case["cohort"] = ("regression" if case["case_id"] in REGRESSION_IDS else
                          "private_repeat" if case["category"] == "explicit_adult_excerpt" else "new_long_development")
        case["grades"] = [dict(c, status="not_run", semantic_passed=None, reviewer=None, notes=None) for c in CANDIDATES]
    manifest.update(schema="echo-summary-round2-preparation-v1", contract=RECIPE,
        evaluation_kind="development_with_separate_regression_and_new_long_cohorts_not_blind",
        candidates=[dict(c, account_access_checked=False) for c in CANDIDATES],
        planned_comparison_cells=24, prepared_comparison_cells=len(writer["cases"]) * 2,
        prompt_sha256=hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        response_schema_sha256=digest(SUMMARY_SCHEMA),
        regression_fixture_sha256=digest(previous.load_cases()), new_fixture_sha256=digest(cases[6:]),
        cohort_case_ids={name: [c["case_id"] for c in review["cases"] if c["cohort"] == name]
                         for name in ("regression", "new_long_development", "private_repeat")},
        long_visible_ratio_target=0.60, first_round_preserved=True)
    manifest["files_sha256"] = {"writer_inputs.json": digest(writer), "reviewer_sheet.json": digest(review)}
    return bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-private-excerpts", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        packet = json.loads(previous.PRIVATE_FIXTURE.read_text(encoding="utf-8")) if args.include_private_excerpts else None
        bundle = build_bundle(packet)
        if args.output_dir:
            previous.prepare(args.output_dir, bundle)
        m = bundle["manifest.json"]
        print(json.dumps({k: m[k] for k in ("case_count", "record_count", "source_chars", "cohort_case_ids",
                                          "prepared_comparison_cells", "model_calls", "production_accepted")}))
        return 0
    except (OSError, ValueError):
        print(json.dumps({"status": "error", "code": "round2_preparation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
