#!/usr/bin/env python3
"""Echo Pact M5-05 只读审计 CLI（不迁移、不写库、不联网）。

用法示例：
    python3 scripts/audit_cli.py --db-path /path/to/memory.db \
        who-can-read rec-001
    python3 scripts/audit_cli.py --db-path /path/to/memory.db \
        who-can-read rec-001 --agent agt-chen
    python3 scripts/audit_cli.py --db-path /path/to/memory.db \
        list-events rec-001
    python3 scripts/audit_cli.py --db-path /path/to/memory.db \
        agent-status agt-chen

退出码：0 成功；2 用法/输入错误（含对象不存在）；3 环境错误
（库缺失/不可读/未到 v5）。所有错误消息走 stderr，不产生 traceback。

脱敏规则见 backend/memory/audit.py 模块 docstring：永不输出
secret/token/secret_hash/salt_hex/params_json 与记录正文。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# 允许从仓库根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.memory.audit import (  # noqa: E402
    AuditEnvironmentError,
    AuditNotFound,
    agent_status,
    list_events,
    who_can_read,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit_cli",
        description="Echo Pact 只读审计（只读打开，不迁移不写库）",
    )
    parser.add_argument(
        "--db-path", required=True,
        help="records_v1 数据库路径（只读打开）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("who-can-read", help="派生记录当前可见性")
    p.add_argument("record_id")
    p.add_argument("--agent", default=None, help="叠加指定 agent 的判定")

    p = sub.add_parser("list-events", help="列出记录的完整可见性事件流")
    p.add_argument("record_id")

    p = sub.add_parser("agent-status", help="查看 agent 状态与凭证清单（脱敏）")
    p.add_argument("agent_id")

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "who-can-read":
            result = who_can_read(args.db_path, args.record_id, args.agent)
        elif args.command == "list-events":
            result = list_events(args.db_path, args.record_id)
        elif args.command == "agent-status":
            result = agent_status(args.db_path, args.agent_id)
        else:  # pragma: no cover - argparse 已拦截
            raise AuditNotFound(f"未知命令: {args.command}")
    except AuditNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except AuditEnvironmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except sqlite3.Error as exc:
        # 连接成功后才发现的损坏/结构异常也归为环境错误，不吐 traceback。
        print(f"error: 数据库结构不可读: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
