"""M5-05 只读审计查询核心：who-can-read / list-events / agent-status。

红线（与工单一致）：
- 只读：mode=ro 打开 + PRAGMA query_only；绝不迁移、绝不写库；
- 脱敏：绝不输出 secret / token / secret_hash / salt_hex / params_json；
  记录正文不输出，只给 content_sha256 前 12 位定位指纹；短指纹不作为
  内容保密性或完整性的证明；
- cred_id 是定位符而非秘密（token = cred_id.secret，仅 cred_id 无法认证），
  审计输出允许完整出现；agent_id / display_name / actor 为审计归因字段，
  原样输出。

退出码语义（由 CLI 层兑现）：
- 0 成功；2 用法或输入校验错误（含记录/agent/凭证不存在）；
- 3 环境错误（库文件不存在、不可读、未迁移到 v5、文件损坏）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .identity import (
    _agent_state,
    _credential_state,
    can_read_record,
    current_visibility,
    IdentityError,
)

# v5 引入、审计查询依赖的最小表集合
_V5_TABLES = (
    "agents",
    "agent_events",
    "agent_credentials",
    "credential_events",
    "record_visibility_events",
    "import_batches",
    "config_kv",
    "schema_migrations",
)

# 脱敏：这些字段名永不出现在审计输出里（静态扫描测试锁死）
FORBIDDEN_AUDIT_FIELDS = (
    "secret",
    "token",
    "secret_hash",
    "salt_hex",
    "params_json",
)

CONTENT_FINGERPRINT_LEN = 12


class AuditError(Exception):
    """审计查询基类异常。"""


class AuditNotFound(AuditError, ValueError):
    """输入校验/对象不存在 → 退出码 2。"""


class AuditEnvironmentError(AuditError):
    """库文件环境错误 → 退出码 3。"""


def open_readonly(db_path: str) -> sqlite3.Connection:
    """以只读方式打开 records_v1 库；不到 v5 一律拒绝（不迁移）。"""
    if not isinstance(db_path, str) or not db_path.strip():
        raise AuditNotFound("db_path 不能为空")
    path = Path(db_path)
    if not path.is_file():
        raise AuditEnvironmentError(f"数据库文件不存在: {db_path}")
    try:
        # as_uri 会正确转义空格、#、% 等 URI 特殊字符；直接拼 file:{path}
        # 会把合法文件名的一部分误当成 query/fragment。
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise AuditEnvironmentError(f"数据库不可读: {exc}") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "records_v1" not in tables:
            raise AuditEnvironmentError("这不是 records_v1 数据库")
        missing = [t for t in _V5_TABLES if t not in tables]
        if missing:
            raise AuditEnvironmentError(
                "数据库未迁移到 v5（缺少: " + ", ".join(missing) + "）；"
                "审计工具只读不迁移，请先完成迁移"
            )
        applied_v5 = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 5"
        ).fetchone()
        if applied_v5 is None:
            raise AuditEnvironmentError(
                "数据库未迁移到 v5（缺少版本 5 迁移记录）；"
                "审计工具只读不迁移，请先完成迁移"
            )
        return conn
    except AuditEnvironmentError:
        conn.close()
        raise
    except sqlite3.Error as exc:
        conn.close()
        raise AuditEnvironmentError(f"数据库结构不可读: {exc}") from exc
    except Exception:
        conn.close()
        raise


def _fingerprint(content_sha256: Optional[str]) -> Optional[str]:
    if not content_sha256:
        return None
    return content_sha256[:CONTENT_FINGERPRINT_LEN]


def _record_row(conn: sqlite3.Connection, record_id: str) -> sqlite3.Row:
    if not isinstance(record_id, str) or not record_id.strip():
        raise AuditNotFound("record_id 不能为空")
    row = conn.execute(
        "SELECT id, record_id, created_at, source_kind, content_sha256 "
        "FROM records_v1 WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if row is None:
        raise AuditNotFound(f"记录不存在: {record_id}")
    return row


def who_can_read(
    db_path: str, record_id: str, agent_id: Optional[str] = None
) -> Dict[str, Any]:
    """派生某记录的当前可见性；可选叠加指定 agent 的判定与依据。"""
    conn = open_readonly(db_path)
    try:
        row = _record_row(conn, record_id)
        visibility = current_visibility(conn, row["id"])
        result: Dict[str, Any] = {
            "record_id": row["record_id"],
            "created_at": row["created_at"],
            "source_kind": row["source_kind"],
            "content_fingerprint": _fingerprint(row["content_sha256"]),
            "owner": visibility["owner"],
            "scope": visibility["scope"],
            "epoch": visibility["epoch"],
            "private_epoch": visibility["private_epoch"],
            "grants": sorted(visibility["grants"]),
        }
        if agent_id is not None:
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise AuditNotFound("agent_id 不能为空")
            state = _agent_state(conn, agent_id)
            try:
                can = can_read_record(conn, agent_id, row["id"])
            except IdentityError:
                # 未注册或已停用 agent 必须与正式读取路径一样失败关闭。
                can = False
            if agent_id == visibility["owner"]:
                via = "owner"
            elif visibility["scope"] == "shared":
                via = "scope_shared"
            elif agent_id in visibility["grants"]:
                via = "grant"
            else:
                via = "none"
            result["agent_check"] = {
                "agent_id": agent_id,
                "agent_state": state if state is not None else "not_registered",
                "can_read": can,
                "via": via,
            }
        return result
    finally:
        conn.close()


def list_events(db_path: str, record_id: str) -> Dict[str, Any]:
    """某记录的完整可见性事件流（按 event_seq 升序，可回溯）。"""
    conn = open_readonly(db_path)
    try:
        row = _record_row(conn, record_id)
        events: List[Dict[str, Any]] = []
        for event in conn.execute(
            "SELECT event_seq, event_kind, target_agent, target_event_seq, "
            "actor, idempotency_key, created_at "
            "FROM record_visibility_events WHERE record_rowid = ? "
            "ORDER BY event_seq",
            (row["id"],),
        ).fetchall():
            events.append(
                {
                    "event_seq": event["event_seq"],
                    "event_kind": event["event_kind"],
                    "target_agent": event["target_agent"],
                    "target_event_seq": event["target_event_seq"],
                    "actor": event["actor"],
                    "idempotency_key": event["idempotency_key"],
                    "created_at": event["created_at"],
                }
            )
        return {
            "record_id": row["record_id"],
            "content_fingerprint": _fingerprint(row["content_sha256"]),
            "event_count": len(events),
            "events": events,
        }
    finally:
        conn.close()


def agent_status(db_path: str, agent_id: str) -> Dict[str, Any]:
    """某 agent 的注册状态、生命周期事件与凭证清单（脱敏）。"""
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise AuditNotFound("agent_id 不能为空")
    conn = open_readonly(db_path)
    try:
        body = conn.execute(
            "SELECT agent_id, display_name, created_at FROM agents "
            "WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if body is None:
            raise AuditNotFound(f"agent 不存在: {agent_id}")
        events = [
            {
                "event_seq": e["event_seq"],
                "kind": e["kind"],
                "actor": e["actor"],
                "created_at": e["created_at"],
            }
            for e in conn.execute(
                "SELECT event_seq, kind, actor, created_at FROM agent_events "
                "WHERE agent_id = ? ORDER BY event_seq",
                (agent_id,),
            ).fetchall()
        ]
        credentials: List[Dict[str, Any]] = []
        for cred in conn.execute(
            "SELECT cred_id, issued_by, issued_at FROM agent_credentials "
            "WHERE agent_id = ? ORDER BY issued_at, cred_id",
            (agent_id,),
        ).fetchall():
            state = _credential_state(conn, cred["cred_id"])
            rotated_event = conn.execute(
                "SELECT replacement_cred_id, grace_until FROM credential_events "
                "WHERE cred_id = ? AND kind = 'rotated' "
                "ORDER BY event_seq DESC LIMIT 1",
                (cred["cred_id"],),
            ).fetchone()
            entry: Dict[str, Any] = {
                "cred_id": cred["cred_id"],
                "state": state["state"] if state else "unknown",
                "issued_by": cred["issued_by"],
                "issued_at": cred["issued_at"],
            }
            if rotated_event is not None:
                entry["replacement_cred_id"] = rotated_event[
                    "replacement_cred_id"
                ]
                entry["grace_until"] = rotated_event["grace_until"]
            credentials.append(entry)
        return {
            "agent_id": body["agent_id"],
            "display_name": body["display_name"],
            "registered_at": body["created_at"],
            "state": _agent_state(conn, agent_id),
            "lifecycle_events": events,
            "credentials": credentials,
        }
    finally:
        conn.close()
