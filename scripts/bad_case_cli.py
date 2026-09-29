#!/usr/bin/env python3
"""Manage private Echo bad-case cards and deterministic recall replays."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.bad_cases.registry import (  # noqa: E402
    BadCaseRegistry,
    BadCaseRegistryError,
    ISSUE_FAMILIES,
    LAYER_VALUES,
    STATUS_VALUES,
)
from backend.memory.recall_debug import (  # noqa: E402
    load_speech_query_policy,
    run_recall_debug,
)


def _add_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bad-case-dir", required=True)
    parser.add_argument("--registry-dir", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="结构化 Echo 私人错题，保留原卷并生成可回归的诊断卡。"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="为未登记原卷创建旁路诊断卡")
    _add_roots(initialize)

    report = commands.add_parser("report", help="汇总状态、问题类型与最近复测")
    _add_roots(report)
    report.add_argument("--include-query", action="store_true")

    review = commands.add_parser("review", help="人工复核分类、期待和修复状态")
    _add_roots(review)
    review.add_argument("case_id")
    review.add_argument("--status", choices=sorted(STATUS_VALUES))
    review.add_argument("--family", action="append", choices=sorted(ISSUE_FAMILIES))
    review.add_argument("--layer", action="append", choices=sorted(LAYER_VALUES))
    review.add_argument("--expect-any", action="append")
    review.add_argument("--expect-all", action="append")
    review.add_argument("--forbid", action="append")
    review.add_argument("--fix-ref")
    review.add_argument("--summary")
    review.add_argument("--note", default="")

    replay = commands.add_parser("replay", help="用当前只读召回器复测旧题")
    _add_roots(replay)
    replay.add_argument("case_id", help="单个 case_id，或 all")
    replay.add_argument("--db", required=True)
    replay.add_argument("--agent-id", required=True)
    replay.add_argument("--limit", type=int, default=10)
    replay.add_argument("--as-of")
    replay.add_argument("--speech-policy")
    replay.add_argument("--no-projection", action="store_true")
    return parser


def _registry(args: argparse.Namespace) -> BadCaseRegistry:
    return BadCaseRegistry(
        args.bad_case_dir,
        args.registry_dir,
        forbidden_root=REPO_ROOT,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry = _registry(args)
        if args.command == "init":
            result = registry.initialize()
        elif args.command == "report":
            result = registry.report(include_queries=args.include_query)
        elif args.command == "review":
            result = registry.review(
                args.case_id,
                status=args.status,
                issue_families=args.family,
                layers=args.layer,
                must_recall_any=args.expect_any,
                must_recall_all=args.expect_all,
                must_not_recall=args.forbid,
                fix_ref=args.fix_ref,
                summary=args.summary,
                note=args.note,
            )
        else:
            policy = (
                load_speech_query_policy(args.speech_policy)
                if args.speech_policy
                else None
            )

            def runner(query: str) -> dict:
                return run_recall_debug(
                    query,
                    db_path=args.db,
                    agent_id=args.agent_id,
                    limit=args.limit,
                    as_of=args.as_of,
                    include_projection=not args.no_projection,
                    speech_query_policy=policy,
                )

            case_ids = (
                [card["case_id"] for card in registry.cards()]
                if args.case_id == "all"
                else [args.case_id]
            )
            result = {
                "replayed": [
                    registry.replay(case_id, runner=runner) for case_id in case_ids
                ]
            }
    except (BadCaseRegistryError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
