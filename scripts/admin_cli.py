#!/usr/bin/env python3
"""Echo Pact M5-04 本地身份管理 CLI（不开放任何网络管理写入口）。

裁定 1：--actor 只是审计归因，不是认证；本工具只能在能直接读写数据库
文件的本机上运行，管理操作不进 HTTP API。

用法示例：
    python3 scripts/admin_cli.py --db-path /path/to/memory.db --yes \
        register-agent --agent-id agt-chen --display-name "沈先生" --actor 馆长
    python3 scripts/admin_cli.py --db-path /path/to/memory.db --yes \
        issue-credential --agent-id agt-chen --actor 馆长

注意：issue-credential / rotate-credential 的明文 secret 只在 stdout 出现
一次，请立即妥善保存；日志与 shell history 都会留下痕迹，自行评估。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从仓库根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.memory.identity import (  # noqa: E402
    IdentityError,
    grant_access,
    issue_credential,
    register_agent,
    revoke_access,
    revoke_credential,
    rotate_credential,
    set_agent_enabled,
    set_owner,
    set_scope,
)


def _emit(payload) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _emit_credential(payload) -> int:
    """Print the bearer token once; do not duplicate its secret component."""
    safe = {key: value for key, value in payload.items() if key != "secret"}
    return _emit(safe)


def _confirm(args) -> None:
    """Require explicit acknowledgement before a local management write."""
    if not args.yes:
        raise IdentityError("管理写操作必须显式传入 --yes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="admin_cli",
        description="Echo Pact 本地身份/授权管理（--actor 为审计归因，非认证）",
    )
    parser.add_argument(
        "--db-path", required=True,
        help="records_v1 数据库路径（管理写操作必须显式指定）",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="确认执行本地管理写操作",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_actor(p):
        p.add_argument("--actor", required=True,
                       help="审计归因（谁在本机执行了此操作）")

    p = sub.add_parser(
        "register-agent",
        help="注册新 agent（agent_id 与 display_name 均为不可变登记）",
    )
    p.add_argument("--agent-id", required=True)
    p.add_argument("--display-name", required=True)
    add_actor(p)

    p = sub.add_parser("disable-agent", help="停用 agent（数据不动）")
    p.add_argument("--agent-id", required=True)
    add_actor(p)

    p = sub.add_parser("enable-agent", help="恢复 agent")
    p.add_argument("--agent-id", required=True)
    add_actor(p)

    p = sub.add_parser("issue-credential", help="签发凭证（明文只出现一次）")
    p.add_argument("--agent-id", required=True)
    add_actor(p)

    p = sub.add_parser("rotate-credential", help="轮换凭证（旧凭证 24h 宽限）")
    p.add_argument("--cred-id", required=True)
    add_actor(p)

    p = sub.add_parser("revoke-credential", help="吊销凭证（终态不可逆）")
    p.add_argument("--cred-id", required=True)
    add_actor(p)

    p = sub.add_parser(
        "set-owner",
        help=(
            "转移记录归属并开启新授权 epoch；即使 owner 相同也会作废旧 grant"
        ),
    )
    p.add_argument("--record-id", required=True)
    p.add_argument("--new-owner", required=True)
    add_actor(p)

    p = sub.add_parser("set-scope", help="设置记录 scope（private/shared）")
    p.add_argument("--record-id", required=True)
    p.add_argument("--scope", required=True, choices=["private", "shared"])
    add_actor(p)

    p = sub.add_parser("grant", help="授权某 agent 可见某记录")
    p.add_argument("--record-id", required=True)
    p.add_argument("--target-agent", required=True)
    add_actor(p)

    p = sub.add_parser("revoke", help="定向撤销授权（需目标 grant 事件序号）")
    p.add_argument("--record-id", required=True)
    p.add_argument("--target-agent", required=True)
    p.add_argument("--target-event-seq", required=True, type=int)
    add_actor(p)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db = args.db_path
    try:
        _confirm(args)
        if args.command == "register-agent":
            return _emit(register_agent(
                args.agent_id, args.display_name,
                actor=args.actor, db_path=db))
        if args.command == "disable-agent":
            return _emit(set_agent_enabled(
                args.agent_id, False, actor=args.actor, db_path=db))
        if args.command == "enable-agent":
            return _emit(set_agent_enabled(
                args.agent_id, True, actor=args.actor, db_path=db))
        if args.command == "issue-credential":
            result = issue_credential(
                args.agent_id, actor=args.actor, db_path=db)
            print("⚠️  凭证明文仅显示这一次，请立即保存：", file=sys.stderr)
            return _emit_credential(result)
        if args.command == "rotate-credential":
            result = rotate_credential(
                args.cred_id, actor=args.actor, db_path=db)
            print("⚠️  新凭证明文仅显示这一次，请立即保存：", file=sys.stderr)
            return _emit_credential(result)
        if args.command == "revoke-credential":
            return _emit(revoke_credential(
                args.cred_id, actor=args.actor, db_path=db))
        if args.command == "set-owner":
            return _emit(set_owner(
                args.record_id, args.new_owner,
                actor=args.actor, db_path=db))
        if args.command == "set-scope":
            return _emit(set_scope(
                args.record_id, args.scope,
                actor=args.actor, db_path=db))
        if args.command == "grant":
            return _emit(grant_access(
                args.record_id, args.target_agent,
                actor=args.actor, db_path=db))
        if args.command == "revoke":
            return _emit(revoke_access(
                args.record_id, args.target_agent, args.target_event_seq,
                actor=args.actor, db_path=db))
    except (IdentityError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
