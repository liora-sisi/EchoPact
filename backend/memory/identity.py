"""M5-04: 身份、凭证与证据可见范围（记录 ACL）。

硬边界（v3 定稿 + 施工裁定）：
- records_v1 证据协议零改动；身份/授权全部在 v5 新增通用表；
- 可见性 = 事件流重放派生，不存可漂移状态；归属转移开启新授权 epoch；
- 凭证格式 cred_id.secret：按 cred_id 定位单行，stdlib scrypt 定参验证；
  无效 cred_id 与错 secret 统一 401 并执行 dummy KDF；
- 可见性 helper 先验证 agent active，disabled 一律拒绝；
- 管理操作仅本地入口（本模块函数），不挂载任何公网路由；
- legacy 锚点 agt-legacy 在迁移时一次性写入，config_kv 禁止 UPDATE/DELETE。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

from .projection import _canonical_hash, _require_agent_id, _require_non_empty_string
from .records_v1 import _connect, _resolve_db_path, migrate_records_db

IDENTITY_SCHEMA_VERSION = "echo-pact-identity-v1"
LEGACY_PRINCIPAL = "agt-legacy"

SCRYPT_PARAMS = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 64}
_DUMMY_SALT = b"echopact-dummy-kdf-salt-v1"
ROTATION_GRACE_HOURS = 24

VISIBILITY_KINDS = ("set_owner", "scope_private", "scope_shared", "grant", "revoke")
CREDENTIAL_EVENT_KINDS = ("issued", "rotated", "revoked", "expired")
AGENT_EVENT_KINDS = ("registered", "disabled", "re-enabled")


class IdentityError(ValueError):
    """身份/授权违规（不泄露目标存在性的统一基类）。"""


class EvidenceNotVisible(IdentityError):
    """Claim 存在当前 principal 不可见的证据。"""


# ---------- 凭证 ----------


def _scrypt_hash(secret: str, salt: bytes, params: Dict[str, int]) -> str:
    return hashlib.scrypt(
        secret.encode("utf-8"), salt=salt,
        n=params["n"], r=params["r"], p=params["p"], dklen=params["dklen"],
    ).hex()


def _dummy_kdf() -> None:
    """无效 cred_id 路径上的时间拉平：照常执行一次同参数 scrypt。"""
    hashlib.scrypt(
        b"dummy-secret", salt=_DUMMY_SALT,
        n=SCRYPT_PARAMS["n"], r=SCRYPT_PARAMS["r"], p=SCRYPT_PARAMS["p"],
        dklen=SCRYPT_PARAMS["dklen"],
    )


def issue_credential(
    agent_id: str, *, actor: str, db_path: Optional[str] = None
) -> Dict[str, Any]:
    """签发凭证（本地管理入口）。明文只在返回值里出现一次。"""
    agent_id = _require_agent_id(agent_id)
    actor = _require_non_empty_string(actor, "actor")
    migrate_records_db(db_path)
    conn = _connect(_resolve_db_path(db_path))
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_agent_active(conn, agent_id)
        cred_id = "cred-" + secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        conn.execute(
            "INSERT INTO agent_credentials "
            "(cred_id, agent_id, kdf, params_json, salt_hex, secret_hash, "
            " issued_by, issued_at) VALUES (?, ?, 'scrypt', ?, ?, ?, ?, ?)",
            (cred_id, agent_id, json.dumps(SCRYPT_PARAMS, sort_keys=True),
             salt.hex(), _scrypt_hash(secret, salt, SCRYPT_PARAMS),
             actor, now),
        )
        conn.execute(
            "INSERT INTO credential_events (cred_id, kind, actor, created_at) "
            "VALUES (?, 'issued', ?, ?)",
            (cred_id, actor, now),
        )
        conn.execute("COMMIT")
        return {
            "cred_id": cred_id,
            "secret": secret,
            "token": f"{cred_id}.{secret}",
            "agent_id": agent_id,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _credential_state(conn, cred_id: str) -> Optional[Dict[str, Any]]:
    body = conn.execute(
        "SELECT * FROM agent_credentials WHERE cred_id = ?", (cred_id,)
    ).fetchone()
    if body is None:
        return None
    events = conn.execute(
        "SELECT kind, replacement_cred_id, grace_until, created_at "
        "FROM credential_events WHERE cred_id = ? ORDER BY event_seq",
        (cred_id,),
    ).fetchall()
    state = "active"
    grace_until = None
    for event in events:
        # revoked / expired are terminal for this credential subject. A
        # malformed or future writer may append later events, but replay must
        # still fail closed instead of reactivating the credential.
        if state in ("revoked", "expired"):
            continue
        if event["kind"] == "issued":
            state = "active"
        elif event["kind"] == "rotated":
            state = "rotated"
            grace_until = event["grace_until"]
        elif event["kind"] in ("revoked", "expired"):
            state = event["kind"]  # 终态不可逆：后续 issued 不改变
            grace_until = None
    return {"body": body, "state": state, "grace_until": grace_until}


def rotate_credential(
    cred_id: str, *, actor: str, db_path: Optional[str] = None
) -> Dict[str, Any]:
    """轮换：签发新凭证；旧凭证标记 rotated，grace_until 算死写入事件。"""
    cred_id = _require_non_empty_string(cred_id, "cred_id")
    actor = _require_non_empty_string(actor, "actor")
    migrate_records_db(db_path)
    conn = _connect(_resolve_db_path(db_path))
    now = datetime.now(timezone.utc)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _credential_state(conn, cred_id)
        if current is None or current["state"] != "active":
            raise IdentityError("凭证不存在或不可轮换")
        agent_id = current["body"]["agent_id"]
        _require_agent_active(conn, agent_id)
        new_cred_id = "cred-" + secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        conn.execute(
            "INSERT INTO agent_credentials "
            "(cred_id, agent_id, kdf, params_json, salt_hex, secret_hash, "
            " issued_by, issued_at) VALUES (?, ?, 'scrypt', ?, ?, ?, ?, ?)",
            (new_cred_id, agent_id, json.dumps(SCRYPT_PARAMS, sort_keys=True),
             salt.hex(), _scrypt_hash(secret, salt, SCRYPT_PARAMS),
             actor, now.isoformat()),
        )
        conn.execute(
            "INSERT INTO credential_events (cred_id, kind, actor, created_at) "
            "VALUES (?, 'issued', ?, ?)",
            (new_cred_id, actor, now.isoformat()),
        )
        grace_until = (now + timedelta(hours=ROTATION_GRACE_HOURS)).isoformat()
        conn.execute(
            "INSERT INTO credential_events "
            "(cred_id, kind, replacement_cred_id, grace_until, actor, created_at) "
            "VALUES (?, 'rotated', ?, ?, ?, ?)",
            (cred_id, new_cred_id, grace_until, actor, now.isoformat()),
        )
        conn.execute("COMMIT")
        return {
            "cred_id": new_cred_id, "secret": secret,
            "token": f"{new_cred_id}.{secret}", "agent_id": agent_id,
            "previous_cred_id": cred_id, "grace_until": grace_until,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def revoke_credential(
    cred_id: str, *, actor: str, db_path: Optional[str] = None
) -> Dict[str, Any]:
    cred_id = _require_non_empty_string(cred_id, "cred_id")
    actor = _require_non_empty_string(actor, "actor")
    migrate_records_db(db_path)
    conn = _connect(_resolve_db_path(db_path))
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _credential_state(conn, cred_id)
        if current is None:
            raise IdentityError("凭证不存在")
        if current["state"] in ("revoked", "expired"):
            conn.execute("COMMIT")
            return {"cred_id": cred_id, "state": current["state"],
                    "idempotent_replay": True}
        conn.execute(
            "INSERT INTO credential_events (cred_id, kind, actor, created_at) "
            "VALUES (?, 'revoked', ?, ?)",
            (cred_id, actor, now),
        )
        conn.execute("COMMIT")
        return {"cred_id": cred_id, "state": "revoked",
                "idempotent_replay": False}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def verify_credential(token: str, db_path: Optional[str] = None) -> Optional[str]:
    """cred_id.secret → agent_id；任何失败统一返回 None（调用方转 401）。

    无效 cred_id 也执行 dummy KDF，时间侧信道拉平。
    rotated 凭证在 grace_until 前仍可验证；过期只读路径派生 expired 事件语义
    （不后台写库，验证时按时间比较判定）。
    """
    if not isinstance(token, str) or "." not in token:
        return None
    cred_id, secret = token.split(".", 1)
    if not cred_id.startswith("cred-") or not secret:
        return None
    migrate_records_db(db_path)
    with closing(_connect(_resolve_db_path(db_path))) as conn:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            "SELECT * FROM agent_credentials WHERE cred_id = ?", (cred_id,)
        ).fetchone()
        if row is None:
            _dummy_kdf()
            return None
        state = _credential_state(conn, cred_id)
        try:
            params = json.loads(row["params_json"])
            if params != SCRYPT_PARAMS:
                return None
            salt = bytes.fromhex(row["salt_hex"])
            if len(salt) != 16:
                return None
            candidate = _scrypt_hash(secret, salt, params)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not hmac.compare_digest(candidate, row["secret_hash"]):
            return None
        if state["state"] == "active":
            pass
        elif state["state"] == "rotated":
            grace = state["grace_until"]
            try:
                grace_at = datetime.fromisoformat(
                    grace.replace("Z", "+00:00")
                ) if grace is not None else None
            except (AttributeError, ValueError):
                return None
            if grace_at is None or grace_at.tzinfo is None:
                return None
            if datetime.now(timezone.utc) >= grace_at.astimezone(timezone.utc):
                return None
        else:
            return None
        try:
            _require_agent_active(conn, row["agent_id"])
        except IdentityError:
            return None
        return row["agent_id"]


# ---------- Agent 生命周期 ----------


def _agent_state(conn, agent_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        return None
    events = conn.execute(
        "SELECT kind FROM agent_events WHERE agent_id = ? "
        "ORDER BY event_seq",
        (agent_id,),
    ).fetchall()
    state = "disabled"
    registered = False
    for event in events:
        kind = event["kind"]
        if kind == "registered" and not registered:
            registered = True
            state = "active"
        elif kind == "disabled" and registered:
            state = "disabled"
        elif kind == "re-enabled" and registered and state == "disabled":
            state = "active"
    return state


def _require_agent_active(conn, agent_id: str) -> None:
    state = _agent_state(conn, agent_id)
    if state is None:
        raise IdentityError("agent 不存在")
    if state != "active":
        raise IdentityError("agent 已停用")


def register_agent(
    agent_id: str, display_name: str, *, actor: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    agent_id = _require_agent_id(agent_id)
    display_name = _require_non_empty_string(display_name, "display_name")
    actor = _require_non_empty_string(actor, "actor")
    migrate_records_db(db_path)
    conn = _connect(_resolve_db_path(db_path))
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _agent_state(conn, agent_id) is not None:
            raise IdentityError("agent 已存在")
        conn.execute(
            "INSERT INTO agents (agent_id, display_name, created_at) "
            "VALUES (?, ?, ?)",
            (agent_id, display_name, now),
        )
        conn.execute(
            "INSERT INTO agent_events (agent_id, kind, actor, idempotency_key, "
            "created_at) VALUES (?, 'registered', ?, ?, ?)",
            (agent_id, actor,
             "agent-register-" + _canonical_hash([agent_id])[:24], now),
        )
        conn.execute("COMMIT")
        return {"agent_id": agent_id, "state": "active"}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def set_agent_enabled(
    agent_id: str, enabled: bool, *, actor: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """停用/恢复（本地管理入口）。数据一行不动。"""
    agent_id = _require_agent_id(agent_id)
    actor = _require_non_empty_string(actor, "actor")
    migrate_records_db(db_path)
    conn = _connect(_resolve_db_path(db_path))
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = _agent_state(conn, agent_id)
        if state is None:
            raise IdentityError("agent 不存在")
        target = "re-enabled" if enabled else "disabled"
        current = "active" if state == "active" else "disabled"
        if (enabled and current == "active") or (not enabled and current == "disabled"):
            conn.execute("COMMIT")
            return {"agent_id": agent_id, "state": current,
                    "idempotent_replay": True}
        conn.execute(
            "INSERT INTO agent_events (agent_id, kind, actor, idempotency_key, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (agent_id, target, actor,
             f"agent-{target}-" + _canonical_hash(
                 [agent_id, target, now])[:24], now),
        )
        conn.execute("COMMIT")
        return {"agent_id": agent_id,
                "state": "active" if enabled else "disabled",
                "idempotent_replay": False}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 可见性派生（唯一实现） ----------


_VISIBLE_RECORD_ROWIDS_SQL = """
SELECT visible_record.id
FROM records_v1 AS visible_record
CROSS JOIN (
    SELECT ? AS agent_id, ? AS legacy_owner
) AS visibility_principal
WHERE COALESCE(
        (
            SELECT event.target_agent
            FROM record_visibility_events AS event
            WHERE event.record_rowid = visible_record.id
              AND event.event_kind = 'set_owner'
            ORDER BY event.event_seq DESC
            LIMIT 1
        ),
        (SELECT value FROM config_kv WHERE key = 'legacy_principal'),
        visibility_principal.legacy_owner
      ) = visibility_principal.agent_id
   OR COALESCE(
        (
            SELECT event.event_kind
            FROM record_visibility_events AS event
            WHERE event.record_rowid = visible_record.id
              AND event.event_kind IN ('scope_private', 'scope_shared')
            ORDER BY event.event_seq DESC
            LIMIT 1
        ),
        'scope_private'
      ) = 'scope_shared'
   OR EXISTS (
        SELECT 1
        FROM record_visibility_events AS grant_event
        WHERE grant_event.record_rowid = visible_record.id
          AND grant_event.event_kind = 'grant'
          AND grant_event.target_agent = visibility_principal.agent_id
          AND grant_event.event_seq > COALESCE(
              (
                  SELECT MAX(owner_event.event_seq)
                  FROM record_visibility_events AS owner_event
                  WHERE owner_event.record_rowid = visible_record.id
                    AND owner_event.event_kind = 'set_owner'
              ),
              0
          )
          AND grant_event.event_seq > COALESCE(
              (
                  SELECT MAX(private_event.event_seq)
                  FROM record_visibility_events AS private_event
                  WHERE private_event.record_rowid = visible_record.id
                    AND private_event.event_kind = 'scope_private'
              ),
              0
          )
          AND NOT EXISTS (
              SELECT 1
              FROM record_visibility_events AS revoke_event
              WHERE revoke_event.record_rowid = visible_record.id
                AND revoke_event.event_kind = 'revoke'
                AND revoke_event.target_event_seq = grant_event.event_seq
          )
      )
"""


def _legacy_principal(conn) -> str:
    row = conn.execute(
        "SELECT value FROM config_kv WHERE key = 'legacy_principal'"
    ).fetchone()
    return row["value"] if row else LEGACY_PRINCIPAL


def current_visibility(conn, record_rowid: int) -> Dict[str, Any]:
    """修 4 终版规则：epoch=最新 set_owner；grant 须在 epoch 与最近
    scope_private 之后且未被定向 revoke。"""
    exists = conn.execute(
        "SELECT 1 FROM records_v1 WHERE id = ?", (record_rowid,)
    ).fetchone()
    if exists is None:
        raise IdentityError("记录不存在")
    events = conn.execute(
        "SELECT event_seq, event_kind, target_agent, target_event_seq "
        "FROM record_visibility_events WHERE record_rowid = ? "
        "ORDER BY event_seq",
        (record_rowid,),
    ).fetchall()
    owner = _legacy_principal(conn)
    epoch = 0
    scope = "private"
    last_private_seq = 0
    grants: Dict[int, Dict[str, Any]] = {}
    revoked_targets: Set[int] = set()
    for event in events:
        kind = event["event_kind"]
        if kind == "set_owner":
            owner = event["target_agent"]
            epoch = event["event_seq"]
            grants = {}
        elif kind == "scope_private":
            scope = "private"
            last_private_seq = event["event_seq"]
        elif kind == "scope_shared":
            scope = "shared"
        elif kind == "grant":
            grants[event["event_seq"]] = {
                "agent": event["target_agent"], "seq": event["event_seq"],
            }
        elif kind == "revoke":
            revoked_targets.add(event["target_event_seq"])
    effective = {
        g["agent"]
        for seq, g in grants.items()
        if seq > epoch and seq > last_private_seq and seq not in revoked_targets
    }
    return {
        "owner": owner,
        "scope": scope,
        "grants": effective,
        "epoch": epoch,
        "private_epoch": last_private_seq,
    }


def _read_channel(agent_id: str, visibility: Dict[str, Any]) -> Optional[str]:
    """可见性通道的唯一判定源：返回命中通道，未命中返回 None。

    优先级固定为 owner → scope_shared → grant（与事件派生语义一致）。
    布尔判定（能不能读）与通道分类（从哪条通道读）都必须从这里派生，
    禁止在别处另写 owner/scope/grants 判定副本。
    """
    if agent_id == visibility["owner"]:
        return "owner"
    if visibility["scope"] == "shared":
        return "scope_shared"
    if agent_id in visibility["grants"]:
        return "grant"
    return None


def can_read_record(conn, agent_id: str, record_rowid: int) -> bool:
    _require_agent_active(conn, agent_id)
    visibility = current_visibility(conn, record_rowid)
    return _read_channel(agent_id, visibility) is not None


def filter_visible_records(
    conn, agent_id: str, record_rowids: Sequence[int]
) -> Set[int]:
    """批量可见性过滤（单次往返逐条派生，禁止 N+1 语义分叉）。"""
    _require_agent_active(conn, agent_id)
    return {
        rowid for rowid in record_rowids
        if _can_read_derived(agent_id, current_visibility(conn, rowid))
    }


def _can_read_derived(agent_id: str, visibility: Dict[str, Any]) -> bool:
    return _read_channel(agent_id, visibility) is not None


def visible_record_rowids_query(conn, agent_id: str) -> tuple[str, List[Any]]:
    """Return the bounded-parameter SQL translation of evidence visibility.

    The query derives owner, latest scope, owner/private epochs and effective
    grants directly from the append-only event stream.  It intentionally uses
    a constant two parameters instead of materialising one bound variable per
    visible record.  M5-06's truth-table tests lock this SQL path to the same
    outcomes as ``current_visibility`` / ``_read_channel``.
    """

    _require_agent_active(conn, agent_id)
    return _VISIBLE_RECORD_ROWIDS_SQL, [agent_id, LEGACY_PRINCIPAL]


def all_visible_rowids(conn, agent_id: str) -> Set[int]:
    """Compatibility/audit API backed by the scalable SQL visibility path."""

    query, params = visible_record_rowids_query(conn, agent_id)
    return {row["id"] for row in conn.execute(query, params).fetchall()}


def require_all_claim_evidence_visible(
    conn, agent_id: str, claim_rowid: int
) -> None:
    _require_agent_active(conn, agent_id)
    rows = conn.execute(
        "SELECT record_rowid FROM claim_evidence WHERE claim_rowid = ?",
        (claim_rowid,),
    ).fetchall()
    if not rows:
        raise EvidenceNotVisible("Claim 缺少可核验的证据")
    for row in rows:
        if not _can_read_derived(agent_id, current_visibility(conn, row["record_rowid"])):
            raise EvidenceNotVisible("Claim 存在当前不可见的证据")


def visible_coverage(conn, agent_id: str) -> Dict[str, Any]:
    """Return coverage for exactly the evidence visible to ``agent_id``."""
    from .records_v1 import _coverage_from_connection

    query, params = visible_record_rowids_query(conn, agent_id)
    return _coverage_from_connection(
        conn,
        visible_rowids_query=query,
        visibility_params=params,
    )


# ---------- 可见性事件写入（本地管理入口） ----------


def _record_rowid_or_raise(conn, record_id: str) -> int:
    row = conn.execute(
        "SELECT id FROM records_v1 WHERE record_id = ?", (record_id,)
    ).fetchone()
    if row is None:
        raise IdentityError("记录不存在")
    return row["id"]


def _emit_event(
    conn, record_rowid: int, event_kind: str, *,
    target_agent: Optional[str], actor: str,
    target_event_seq: Optional[int], now: str,
    idem_parts: Sequence[Any],
) -> None:
    key = "vis-" + _canonical_hash(
        [IDENTITY_SCHEMA_VERSION, event_kind, *idem_parts]
    )[:24]
    conn.execute(
        "INSERT INTO record_visibility_events "
        "(record_rowid, event_kind, target_agent, actor, target_event_seq, "
        " idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (record_rowid, event_kind, target_agent, actor, target_event_seq,
         key, now),
    )


def set_owner(
    record_id: str, new_owner: str, *, actor: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """归属转移：开启新授权 epoch，旧 owner 的全部 grant 自动失效。"""
    actor = _require_non_empty_string(actor, "actor")
    new_owner = _require_agent_id(new_owner)
    record_id = _require_non_empty_string(record_id, "record_id")
    migrate_records_db(db_path)
    conn = _connect(_resolve_db_path(db_path))
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_agent_active(conn, new_owner)
        rowid = _record_rowid_or_raise(conn, record_id)
        _emit_event(conn, rowid, "set_owner", target_agent=new_owner,
                    actor=actor, target_event_seq=None, now=now,
                    idem_parts=[record_id, new_owner, now])
        conn.execute("COMMIT")
        return {"record_id": record_id, "owner": new_owner}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def set_scope(
    record_id: str, scope: str, *, actor: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    actor = _require_non_empty_string(actor, "actor")
    record_id = _require_non_empty_string(record_id, "record_id")
    if scope not in ("private", "shared"):
        raise IdentityError("scope 必须是 private 或 shared")
    migrate_records_db(db_path)
    conn = _connect(_resolve_db_path(db_path))
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rowid = _record_rowid_or_raise(conn, record_id)
        _emit_event(conn, rowid, f"scope_{scope}", target_agent=None,
                    actor=actor, target_event_seq=None, now=now,
                    idem_parts=[record_id, scope, now])
        conn.execute("COMMIT")
        return {"record_id": record_id, "scope": scope}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def grant_access(
    record_id: str, target_agent: str, *, actor: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    actor = _require_non_empty_string(actor, "actor")
    record_id = _require_non_empty_string(record_id, "record_id")
    target_agent = _require_agent_id(target_agent)
    migrate_records_db(db_path)
    conn = _connect(_resolve_db_path(db_path))
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_agent_active(conn, target_agent)
        rowid = _record_rowid_or_raise(conn, record_id)
        visibility = current_visibility(conn, rowid)
        if target_agent in visibility["grants"]:
            conn.execute("COMMIT")
            return {"record_id": record_id, "granted": target_agent,
                    "idempotent_replay": True}
        _emit_event(conn, rowid, "grant", target_agent=target_agent,
                    actor=actor, target_event_seq=None, now=now,
                    idem_parts=[record_id, target_agent, "grant", now])
        conn.execute("COMMIT")
        return {"record_id": record_id, "granted": target_agent,
                "idempotent_replay": False}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def revoke_access(
    record_id: str, target_agent: str, target_event_seq: int, *,
    actor: str, db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """定向撤销：事务内验证目标是同记录、同 agent、当前 epoch 内且未撤销的 grant。"""
    actor = _require_non_empty_string(actor, "actor")
    record_id = _require_non_empty_string(record_id, "record_id")
    target_agent = _require_agent_id(target_agent)
    if isinstance(target_event_seq, bool) or not isinstance(target_event_seq, int):
        raise IdentityError("target_event_seq 必须是整数")
    migrate_records_db(db_path)
    conn = _connect(_resolve_db_path(db_path))
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rowid = _record_rowid_or_raise(conn, record_id)
        grant = conn.execute(
            "SELECT event_seq, target_agent FROM record_visibility_events "
            "WHERE event_seq = ? AND record_rowid = ? AND event_kind = 'grant'",
            (target_event_seq, rowid),
        ).fetchone()
        if grant is None or grant["target_agent"] != target_agent:
            raise IdentityError("撤销目标不是该记录对该 agent 的授权")
        visibility = current_visibility(conn, rowid)
        prior_revoke = conn.execute(
            "SELECT 1 FROM record_visibility_events "
            "WHERE record_rowid = ? AND event_kind = 'revoke' "
            "AND target_event_seq = ?",
            (rowid, target_event_seq),
        ).fetchone()
        if (
            target_event_seq <= visibility["epoch"]
            or target_event_seq <= visibility["private_epoch"]
            or prior_revoke is not None
        ):
            raise IdentityError("撤销目标不在当前授权 epoch 内或已被撤销")
        _emit_event(conn, rowid, "revoke", target_agent=target_agent,
                    actor=actor, target_event_seq=target_event_seq, now=now,
                    idem_parts=[record_id, target_agent, "revoke",
                                target_event_seq])
        conn.execute("COMMIT")
        return {"record_id": record_id, "revoked": target_agent,
                "target_event_seq": target_event_seq}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
