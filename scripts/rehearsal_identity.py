#!/usr/bin/env python3
"""Echo Pact M5-05 身份全流程彩排（纯合成、无网络、不碰真实数据）。

流程十步：注册 → 签发 → 导入 → 授权 → 归属转移 → 撤销 → 召回 → 投影
→ 冲突呈现 → 输出脱敏审计报告。

红线：
- 只在临时目录自建合成库；刻意不提供 --db-path，从接口上杜绝误碰真实库；
- 不发起任何网络请求；
- 报告脱敏：不出现任何记录正文与凭证材料，证据只以 record_id +
  content_sha256 前 12 位指纹表示。

退出码：0 全部通过；1 任一步骤失败（报告照常输出，含失败细节）；
2 用法错误。

用法：
    python3 scripts/rehearsal_identity.py --out /path/to/report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# 允许从仓库根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.memory import audit  # noqa: E402
from backend.memory.claim_conflicts import register_conflict  # noqa: E402
from backend.memory.identity import (  # noqa: E402
    grant_access,
    issue_credential,
    register_agent,
    revoke_access,
    set_owner,
)
from backend.memory.projection import build_projection, get_claim  # noqa: E402
from backend.memory.recall_projection import recall_with_projection  # noqa: E402
from backend.memory.records_v1 import (  # noqa: E402
    import_record_package,
    recall_records,
)

OWNER = "agt-reh-owner"
PEER = "agt-reh-peer"
STRANGER = "agt-reh-stranger"
R1, R2, R3 = "reh-r1", "reh-r2", "reh-r3"
QUERY = "彩排灯塔"

REPORT_SCHEMA = "echo-pact-m505-rehearsal-v1"


def _write_package(tmp_dir: str) -> str:
    records = [
        {
            "record_id": rid,
            "source_kind": "conversation_export",
            "source_ref": f"synthetic://rehearsal/{rid}",
            "conversation_id": "synthetic-rehearsal",
            "branch_id": "main",
            "message_id": rid,
            "role": "user",
            "content": f"{QUERY}合成证据 {rid}",
            "created_at": f"2026-07-0{i}T00:00:00Z",
            "verified": True,
            "authority": "user-confirmed",
            "source_cutoff_at": "2026-08-01T00:00:00Z",
            "conflict_group_id": None,
        }
        for i, rid in enumerate((R1, R2, R3), start=1)
    ]
    path = Path(tmp_dir) / "rehearsal-package.json"
    path.write_text(
        json.dumps({"schema_version": "echo-pact-records-v1",
                    "records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)


def _build_rehearsal(tmp_dir: str) -> dict:
    """在已隔离的临时目录中执行彩排并构造脱敏报告。"""
    db_path = str(Path(tmp_dir) / "rehearsal.db")
    steps = []
    ctx: dict = {}  # 步骤间共享的合成对象句柄（claim_id 等）

    def step(name):
        def deco(fn):
            try:
                detail = fn() or {}
                steps.append({"step": name, "status": "pass",
                              "detail": detail})
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                for private_value in ctx.get("private_credential_values", []):
                    error = error.replace(private_value, "[REDACTED]")
                steps.append({"step": name, "status": "fail",
                              "detail": {"error": error}})
        return deco

    @step("01-注册")
    def _register():
        for agent_id in (OWNER, PEER, STRANGER):
            register_agent(agent_id, f"彩排 {agent_id}",
                           actor="rehearsal", db_path=db_path)
        states = {a: audit.agent_status(db_path, a)["state"]
                  for a in (OWNER, PEER, STRANGER)}
        assert all(s == "active" for s in states.values()), states
        return {"agents": states}

    @step("02-签发")
    def _issue():
        creds = {}
        private_values = ctx.setdefault("private_credential_values", [])
        for agent_id in (OWNER, PEER):
            issued = issue_credential(agent_id, actor="rehearsal",
                                       db_path=db_path)
            creds[agent_id] = issued["cred_id"]  # 只留 cred_id，secret 不落报告
            private_values.extend((issued["secret"], issued["token"]))
        listed = audit.agent_status(db_path, OWNER)["credentials"]
        assert len(listed) == 1 and listed[0]["state"] == "active", listed
        return {"cred_ids": creds}

    @step("03-导入")
    def _import():
        pkg = _write_package(tmp_dir)
        summary = import_record_package(pkg, db_path=db_path,
                                        owner_agent_id=OWNER,
                                        actor="rehearsal")
        assert summary["added"] == 3, summary
        vis = audit.who_can_read(db_path, R1)
        assert vis["owner"] == OWNER and vis["grants"] == [], vis
        return {"added": summary["added"], "batch_id": summary["batch_id"]}

    @step("04-授权")
    def _grant():
        grant_access(R1, PEER, actor="rehearsal", db_path=db_path)
        check = audit.who_can_read(db_path, R1, agent_id=PEER)["agent_check"]
        assert check["can_read"] is True and check["via"] == "grant", check
        denied = audit.who_can_read(db_path, R1, agent_id=STRANGER)["agent_check"]
        assert denied["can_read"] is False, denied
        return {"peer": check, "stranger": denied}

    @step("05-归属转移")
    def _transfer():
        # 先给 stranger 授权 R2，再转移给 PEER：旧 epoch 授权应作废
        grant_access(R2, STRANGER, actor="rehearsal", db_path=db_path)
        set_owner(R2, PEER, actor="rehearsal", db_path=db_path)
        vis = audit.who_can_read(db_path, R2)
        assert vis["owner"] == PEER and vis["grants"] == [], vis
        stale = audit.who_can_read(db_path, R2, agent_id=STRANGER)["agent_check"]
        assert stale["can_read"] is False, stale
        return {"owner": vis["owner"], "epoch": vis["epoch"],
                "stale_grant_voided": True}

    @step("06-撤销")
    def _revoke():
        grant_access(R3, STRANGER, actor="rehearsal", db_path=db_path)
        events = audit.list_events(db_path, R3)["events"]
        grant_seq = next(e["event_seq"] for e in events
                         if e["event_kind"] == "grant")
        revoke_access(R3, STRANGER, grant_seq,
                      actor="rehearsal", db_path=db_path)
        check = audit.who_can_read(db_path, R3, agent_id=STRANGER)["agent_check"]
        assert check["can_read"] is False, check
        tail = audit.list_events(db_path, R3)["events"][-1]
        assert tail["event_kind"] == "revoke" \
            and tail["target_event_seq"] == grant_seq, tail
        return {"revoked_grant_seq": grant_seq}

    @step("07-召回")
    def _recall():
        as_owner = recall_records(QUERY, db_path=db_path, agent_id=OWNER)
        as_peer = recall_records(QUERY, db_path=db_path, agent_id=PEER)
        as_stranger = recall_records(QUERY, db_path=db_path, agent_id=STRANGER)
        owner_ids = sorted(m["record_id"] for m in as_owner["memories"])
        peer_ids = sorted(m["record_id"] for m in as_peer["memories"])
        assert owner_ids == [R1, R3], owner_ids  # R2 已易主
        assert peer_ids == [R1, R2], peer_ids    # R1 授权 + R2 归属
        assert as_stranger["memories"] == []
        assert as_stranger["coverage"]["coverage_status"] == "no_visible_records"
        return {"owner_sees": owner_ids, "peer_sees": peer_ids,
                "stranger_sees": []}

    @step("08-投影")
    def _projection():
        items = [
            {"claim_key": "reh:c1", "claim_kind": "fact",
             "content": "彩排 claim 一",
             "evidence_record_ids": [R1]},
            {"claim_key": "reh:c2", "claim_kind": "fact",
             "content": "彩排 claim 二",
             "evidence_record_ids": [R2]},
        ]
        built = build_projection(items, agent_id=PEER, rule_id="rehearsal-rule",
                                 db_path=db_path)
        assert built["created"] == 2, built
        try:
            build_projection(
                [{"claim_key": "reh:evil", "claim_kind": "fact",
                  "content": "彩排越权 claim", "evidence_record_ids": [R1]}],
                agent_id=STRANGER, rule_id="rehearsal-rule", db_path=db_path,
            )
        except ValueError:
            denied = True
        else:
            denied = False
        assert denied, "stranger 的越权投影构建未被拦截"
        ctx["claim_ids"] = [c["claim_id"] for c in built["claims"]]
        return {"built": ctx["claim_ids"],
                "stranger_build_denied": denied}

    @step("09-冲突呈现")
    def _conflict():
        # build_projection 幂等：直接复用 08 步构建的 claim 句柄
        claims = ctx["claim_ids"]
        assert len(claims) == 2, claims
        conflict_id = register_conflict(
            "reh:topic", [{"claim_id": c} for c in claims],
            agent_id=PEER, db_path=db_path,
        )["conflict_id"]
        before = recall_with_projection(QUERY, agent_id=PEER, db_path=db_path)
        r1_hit = next(m for m in before["memories"] if m["record_id"] == R1)
        assert r1_hit["claims"][0]["conflicts"][0]["status"] == "open"
        # 撤销 PEER 对 R1 的授权 → C1 证据不可见 → 冲突整组脱敏
        events = audit.list_events(db_path, R1)["events"]
        grant_seq = next(e["event_seq"] for e in events
                         if e["event_kind"] == "grant"
                         and e["target_agent"] == PEER)
        revoke_access(R1, PEER, grant_seq, actor="rehearsal", db_path=db_path)
        after = recall_with_projection(QUERY, agent_id=PEER, db_path=db_path)
        after_ids = [m["record_id"] for m in after["memories"]]
        assert R1 not in after_ids and R2 in after_ids, after_ids
        r2_hit = next(m for m in after["memories"] if m["record_id"] == R2)
        masked = r2_hit["claims"][0]["conflicts"][0]
        assert masked == {"conflict_id": conflict_id, "status": "restricted"}, \
            masked
        # 直读路径同步收口：C1 对 PEER 不再可读
        assert get_claim(claims[0], PEER, db_path=db_path) is None
        return {"conflict_id": conflict_id, "masked_after_revoke": True}

    @step("10-脱敏审计报告")
    def _report():
        snapshots = {
            "records": {
                rid: audit.who_can_read(db_path, rid) for rid in (R1, R2, R3)
            },
            "agents": {
                a: audit.agent_status(db_path, a)
                for a in (OWNER, PEER, STRANGER)
            },
            "event_streams": {
                rid: audit.list_events(db_path, rid)["event_count"]
                for rid in (R1, R2, R3)
            },
        }
        # 脱敏自检：快照里不允许出现记录正文与凭证材料字段
        blob = json.dumps(snapshots, ensure_ascii=False)
        for forbidden in audit.FORBIDDEN_AUDIT_FIELDS:
            assert forbidden not in blob, forbidden
        assert QUERY not in blob, "记录正文泄露进报告"
        return {"snapshot_keys": sorted(snapshots.keys())}

    passed = sum(1 for s in steps if s["status"] == "pass")
    report = {
        "schema_version": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": "temporary synthetic (auto-generated, never --db-path)",
        "network": "none",
        "summary": {"total": len(steps), "passed": passed,
                    "failed": len(steps) - passed},
        "steps": steps,
        "redaction": (
            "报告不含任何记录正文与凭证材料（secret/token/hash/salt）；"
            "证据以 record_id 与 content_sha256 前 12 位指纹表示。"
        ),
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    for private_value in ctx.get("private_credential_values", []):
        assert private_value not in payload, "凭证材料泄露进报告"
    return report


def _write_report_exclusive(out_path: str, report: dict) -> None:
    """只创建新报告；已有目标绝不覆盖，写失败只清理本次半成品。"""
    if not isinstance(out_path, str) or not out_path.strip():
        raise ValueError("out 不能为空")
    path = Path(out_path)
    if path.exists():
        raise FileExistsError(f"输出文件已存在，拒绝覆盖: {out_path}")
    if not path.parent.is_dir():
        raise ValueError(f"输出目录不存在: {path.parent}")
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            handle.write(payload)
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def run_rehearsal(out_path: str) -> dict:
    """执行彩排；返回报告 dict。任一步失败 status=fail，流程继续。"""
    # 在开始生成合成库前就拒绝危险输出目标；随后用 x 模式防竞态覆盖。
    if not isinstance(out_path, str) or not out_path.strip():
        raise ValueError("out 不能为空")
    if Path(out_path).exists():
        raise FileExistsError(f"输出文件已存在，拒绝覆盖: {out_path}")
    with tempfile.TemporaryDirectory(prefix="m505-rehearsal-") as tmp_dir:
        report = _build_rehearsal(tmp_dir)
    _write_report_exclusive(out_path, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="rehearsal_identity",
        description="M5-05 身份全流程彩排（纯合成临时库，无网络）",
    )
    parser.add_argument("--out", required=True, help="脱敏审计报告输出路径")
    args = parser.parse_args(argv)
    try:
        report = run_rehearsal(args.out)
    except (FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: 无法写入彩排报告: {exc}", file=sys.stderr)
        return 1
    summary = report["summary"]
    for step in report["steps"]:
        mark = "PASS" if step["status"] == "pass" else "FAIL"
        print(f"[{mark}] {step['step']}")
    print(f"彩排完成：{summary['passed']}/{summary['total']} 通过，"
          f"报告已写入 {args.out}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
