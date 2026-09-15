#!/usr/bin/env python3
"""Local read-only CLI for explaining one Echo Pact recall query."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.memory.recall_debug import (  # noqa: E402
    load_speech_query_policy,
    render_recall_debug,
    run_recall_debug,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="解释一条 Echo Pact 查询为什么召回或没有召回。"
    )
    parser.add_argument("query", nargs="?", help="要调试的自然语言原话")
    parser.add_argument(
        "--db",
        default=os.getenv("ECHO_PACT_DEBUG_DB_PATH"),
        help="只读 SQLite 路径；也可用 ECHO_PACT_DEBUG_DB_PATH",
    )
    parser.add_argument(
        "--agent-id",
        default=os.getenv("ECHO_PACT_DEBUG_AGENT_ID"),
        help="固定读取身份；也可用 ECHO_PACT_DEBUG_AGENT_ID",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--as-of")
    parser.add_argument(
        "--speech-policy",
        default=os.getenv("ECHO_PACT_DEBUG_SPEECH_POLICY"),
        help="可选的本地 M8 JSON 映射；不会写回数据库",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--no-projection", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    query = args.query or input("请输入要调试的原话：").strip()
    if not query:
        raise SystemExit("Query不能为空")
    if not args.db:
        raise SystemExit("请用 --db 或 ECHO_PACT_DEBUG_DB_PATH 指定只读数据库")
    if not args.agent_id:
        raise SystemExit(
            "请用 --agent-id 或 ECHO_PACT_DEBUG_AGENT_ID 指定固定读取身份"
        )
    policy = (
        load_speech_query_policy(args.speech_policy)
        if args.speech_policy
        else None
    )
    response = run_recall_debug(
        query,
        db_path=args.db,
        agent_id=args.agent_id,
        limit=args.limit,
        as_of=args.as_of,
        include_projection=not args.no_projection,
        speech_query_policy=policy,
    )
    if args.as_json:
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        print(render_recall_debug(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
