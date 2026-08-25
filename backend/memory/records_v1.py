"""Versioned record import and offline recall for Echo Pact V1.

The V1 path is intentionally independent from the legacy ``memories`` and
Chroma paths.  Records and their FTS5 index are changed in the same SQLite
transaction, so an interrupted import can be retried safely.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


RECORD_SCHEMA_VERSION = "echo-pact-records-v1"
COMPACT_RECORD_SCHEMA_VERSION = "echo-pact-records-v2"
SUPPORTED_RECORD_SCHEMA_VERSIONS = {
    RECORD_SCHEMA_VERSION,
    COMPACT_RECORD_SCHEMA_VERSION,
}
RECALL_SCHEMA_VERSION = "echo-pact-recall-v1"
MIGRATION_VERSION = 6

REQUIRED_FIELDS = (
    "record_id",
    "source_kind",
    "source_ref",
    "conversation_id",
    "branch_id",
    "message_id",
    "role",
    "content",
    "created_at",
    "verified",
    "authority",
    "source_cutoff_at",
)

COMPACT_REQUIRED_FIELDS = tuple(
    field for field in REQUIRED_FIELDS if field != "branch_id"
) + ("branch_memberships",)

ALLOWED_ROLES = {"system", "user", "assistant", "tool", "developer"}
AUTHORITATIVE_VALUES = {
    "official",
    "primary",
    "user",
    "user-confirmed",
    "verified-primary",
}


class RecordPackageError(ValueError):
    """The input package is invalid and was not imported."""

    def __init__(self, message: str, *, summary: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.summary = summary or {
            "schema_version": RECORD_SCHEMA_VERSION,
            "added": 0,
            "skipped": 0,
            "failed": 1,
        }


class RecordConflictError(RecordPackageError):
    """An existing record_id has different content."""


@dataclass(frozen=True)
class LoadedRecordPackage:
    records: List[Dict[str, Any]]
    duplicate_skips: int = 0
    schema_version: str = RECORD_SCHEMA_VERSION


def _resolve_db_path(db_path: Optional[str] = None) -> str:
    if db_path:
        return str(Path(db_path))
    # Resolve at call time so the existing test suite can monkeypatch DB_PATH.
    from backend.utils import db as db_module

    return str(db_module.DB_PATH)


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    resolved = _resolve_db_path(db_path)
    parent = Path(resolved).expanduser().resolve().parent
    if not parent.exists():
        raise FileNotFoundError(f"Database parent directory does not exist: {parent}")
    conn = sqlite3.connect(resolved, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _connect_readonly(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a fully read-only records database without running migrations.

    This path is used by external recall adapters such as the MCP gateway.  It
    deliberately refuses an older or newer schema instead of silently changing
    the database.  ``Path.as_uri`` keeps Windows paths containing spaces, ``#``
    or ``%`` from being parsed as URI syntax.
    """

    resolved = Path(_resolve_db_path(db_path)).expanduser()
    if not resolved.is_file():
        raise FileNotFoundError("Echo Pact records database does not exist")
    try:
        uri = resolved.resolve(strict=True).as_uri() + "?mode=ro"
        conn = sqlite3.connect(
            uri, uri=True, timeout=30, isolation_level=None
        )
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError("Echo Pact records database is not readable") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA query_only = ON")
        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        expected = list(range(1, MIGRATION_VERSION + 1))
        if versions != expected:
            raise RuntimeError(
                "Echo Pact records database schema is not the supported version"
            )
        return conn
    except Exception:
        conn.close()
        raise


MIGRATION_1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS records_v1 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version TEXT NOT NULL,
        record_id TEXT NOT NULL UNIQUE,
        source_kind TEXT NOT NULL,
        source_ref TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        branch_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
        authority TEXT NOT NULL,
        source_cutoff_at TEXT NOT NULL,
        conflict_group_id TEXT,
        content_sha256 TEXT NOT NULL,
        imported_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_records_v1_conversation_branch
    ON records_v1 (conversation_id, branch_id, message_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_records_v1_created_at
    ON records_v1 (created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS records_v1_index_state (
        record_rowid INTEGER PRIMARY KEY,
        content_sha256 TEXT NOT NULL,
        indexed_at TEXT NOT NULL,
        FOREIGN KEY (record_rowid) REFERENCES records_v1(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS records_v1_fts USING fts5(
        content,
        content='records_v1',
        content_rowid='id',
        tokenize='trigram'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS records_v1_ai AFTER INSERT ON records_v1 BEGIN
        INSERT INTO records_v1_fts(rowid, content) VALUES (new.id, new.content);
        INSERT INTO records_v1_index_state(record_rowid, content_sha256, indexed_at)
        VALUES (new.id, new.content_sha256, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS records_v1_ad AFTER DELETE ON records_v1 BEGIN
        INSERT INTO records_v1_fts(records_v1_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
        DELETE FROM records_v1_index_state WHERE record_rowid = old.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS records_v1_au AFTER UPDATE ON records_v1 BEGIN
        INSERT INTO records_v1_fts(records_v1_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
        INSERT INTO records_v1_fts(rowid, content) VALUES (new.id, new.content);
        INSERT OR REPLACE INTO records_v1_index_state(
            record_rowid, content_sha256, indexed_at
        ) VALUES (
            new.id, new.content_sha256,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        );
    END
    """,
)

MIGRATION_2_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS records_v1_branch_memberships (
        record_rowid INTEGER NOT NULL,
        branch_id TEXT NOT NULL,
        position INTEGER,
        PRIMARY KEY (record_rowid, branch_id),
        FOREIGN KEY (record_rowid) REFERENCES records_v1(id) ON DELETE CASCADE,
        CHECK (position IS NULL OR position >= 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_records_v1_branch_membership
    ON records_v1_branch_memberships (branch_id, position, record_rowid)
    """,
    """
    INSERT OR IGNORE INTO records_v1_branch_memberships(
        record_rowid, branch_id, position
    )
    SELECT id, branch_id, NULL FROM records_v1
    """,
)


MIGRATION_3_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS projection_runs (
        run_id TEXT PRIMARY KEY,
        rule_id TEXT NOT NULL CHECK (length(trim(rule_id)) > 0),
        rule_version INTEGER NOT NULL CHECK (rule_version >= 1),
        agent_id TEXT NOT NULL CHECK (length(trim(agent_id)) > 0),
        source_hash TEXT NOT NULL CHECK (length(source_hash) = 64),
        claim_count INTEGER NOT NULL CHECK (claim_count >= 0),
        status TEXT NOT NULL CHECK (status IN ('completed')),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projection_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id TEXT NOT NULL,
        claim_version INTEGER NOT NULL CHECK (claim_version >= 1),
        agent_id TEXT NOT NULL CHECK (length(trim(agent_id)) > 0),
        claim_kind TEXT NOT NULL
            CHECK (claim_kind IN ('fact', 'preference', 'relationship', 'task', 'note')),
        content TEXT NOT NULL CHECK (length(trim(content)) > 0),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'superseded', 'sealed')),
        rule_id TEXT NOT NULL CHECK (length(trim(rule_id)) > 0),
        rule_version INTEGER NOT NULL CHECK (rule_version >= 1),
        source_hash TEXT NOT NULL CHECK (length(source_hash) = 64),
        projection_hash TEXT NOT NULL CHECK (length(projection_hash) = 64),
        run_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        superseded_by_version INTEGER,
        CHECK (
            superseded_by_version IS NULL
            OR superseded_by_version > claim_version
        ),
        UNIQUE (claim_id, claim_version),
        FOREIGN KEY (run_id) REFERENCES projection_runs(run_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (claim_id, superseded_by_version)
            REFERENCES projection_claims(claim_id, claim_version)
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_projection_claims_agent_status
    ON projection_claims (agent_id, status, claim_kind)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_projection_claims_one_active
    ON projection_claims (claim_id)
    WHERE status = 'active'
    """,
    """
    CREATE TABLE IF NOT EXISTS claim_evidence (
        claim_rowid INTEGER NOT NULL,
        record_rowid INTEGER NOT NULL,
        link_kind TEXT NOT NULL DEFAULT 'supports',
        PRIMARY KEY (claim_rowid, record_rowid, link_kind),
        FOREIGN KEY (claim_rowid) REFERENCES projection_claims(id)
            ON DELETE RESTRICT,
        FOREIGN KEY (record_rowid) REFERENCES records_v1(id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_claim_evidence_record
    ON claim_evidence (record_rowid)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS records_v1_immutable_update
    BEFORE UPDATE ON records_v1 BEGIN
        SELECT RAISE(
            ABORT,
            'records_v1 evidence is immutable; append a new record instead'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS records_v1_immutable_delete
    BEFORE DELETE ON records_v1 BEGIN
        SELECT RAISE(
            ABORT,
            'records_v1 evidence is immutable; append a tombstone record instead'
        );
    END
    """,
)

MIGRATION_4_STATEMENTS = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_projection_claims_row_agent
    ON projection_claims (id, agent_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS claim_conflicts (
        conflict_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL CHECK (length(trim(agent_id)) > 0),
        topic_key TEXT NOT NULL CHECK (length(trim(topic_key)) > 0),
        created_at TEXT NOT NULL,
        UNIQUE (agent_id, topic_key),
        UNIQUE (conflict_id, agent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conflict_members (
        member_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        conflict_id TEXT NOT NULL,
        agent_id TEXT NOT NULL CHECK (length(trim(agent_id)) > 0),
        claim_rowid INTEGER NOT NULL,
        role TEXT NOT NULL DEFAULT 'contender'
            CHECK (role IN ('contender')),
        added_at TEXT NOT NULL,
        UNIQUE (conflict_id, claim_rowid),
        UNIQUE (conflict_id, agent_id, claim_rowid),
        FOREIGN KEY (conflict_id, agent_id)
            REFERENCES claim_conflicts(conflict_id, agent_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (claim_rowid, agent_id)
            REFERENCES projection_claims(id, agent_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conflict_members_claim
    ON conflict_members (claim_rowid)
    """,
    """
    CREATE TABLE IF NOT EXISTS conflict_decisions (
        decision_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id TEXT NOT NULL UNIQUE,
        conflict_id TEXT NOT NULL,
        agent_id TEXT NOT NULL CHECK (length(trim(agent_id)) > 0),
        decision TEXT NOT NULL CHECK (decision IN
            ('unresolved', 'confirm_claim', 'keep_all', 'invalidate')),
        target_claim_rowid INTEGER,
        rationale TEXT CHECK (rationale IS NULL OR length(rationale) <= 4000),
        decided_by TEXT NOT NULL
            CHECK (length(trim(decided_by)) > 0 AND length(decided_by) <= 128),
        created_at TEXT NOT NULL,
        CHECK (
            (decision = 'confirm_claim' AND target_claim_rowid IS NOT NULL)
            OR (decision != 'confirm_claim' AND target_claim_rowid IS NULL)
        ),
        FOREIGN KEY (conflict_id, agent_id)
            REFERENCES claim_conflicts(conflict_id, agent_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (conflict_id, agent_id, target_claim_rowid)
            REFERENCES conflict_members(conflict_id, agent_id, claim_rowid)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conflict_decisions_conflict
    ON conflict_decisions (conflict_id, decision_seq)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS conflict_members_no_append_after_decision
    BEFORE INSERT ON conflict_members
    WHEN EXISTS (
        SELECT 1 FROM conflict_decisions
        WHERE conflict_id = NEW.conflict_id
    ) BEGIN
        SELECT RAISE(
            ABORT,
            'decided conflict cannot accept new members'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS claim_conflicts_immutable_update
    BEFORE UPDATE ON claim_conflicts BEGIN
        SELECT RAISE(ABORT, 'claim_conflicts is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS claim_conflicts_immutable_delete
    BEFORE DELETE ON claim_conflicts BEGIN
        SELECT RAISE(ABORT, 'claim_conflicts is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS conflict_members_immutable_update
    BEFORE UPDATE ON conflict_members BEGIN
        SELECT RAISE(ABORT, 'conflict_members is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS conflict_members_immutable_delete
    BEFORE DELETE ON conflict_members BEGIN
        SELECT RAISE(ABORT, 'conflict_members is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS conflict_decisions_immutable_update
    BEFORE UPDATE ON conflict_decisions BEGIN
        SELECT RAISE(ABORT, 'conflict_decisions is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS conflict_decisions_immutable_delete
    BEFORE DELETE ON conflict_decisions BEGIN
        SELECT RAISE(ABORT, 'conflict_decisions is append-only');
    END
    """,
)


MIGRATION_5_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS agents (
        agent_id TEXT PRIMARY KEY CHECK (length(trim(agent_id)) > 0),
        display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_events (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('registered', 'disabled', 're-enabled')),
        actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_credentials (
        cred_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        kdf TEXT NOT NULL CHECK (kdf = 'scrypt'),
        params_json TEXT NOT NULL,
        salt_hex TEXT NOT NULL,
        secret_hash TEXT NOT NULL,
        issued_by TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS credential_events (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        cred_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('issued', 'rotated', 'revoked', 'expired')),
        replacement_cred_id TEXT,
        grace_until TEXT,
        actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
        created_at TEXT NOT NULL,
        FOREIGN KEY (cred_id) REFERENCES agent_credentials(cred_id) ON DELETE RESTRICT,
        FOREIGN KEY (replacement_cred_id) REFERENCES agent_credentials(cred_id)
            ON DELETE RESTRICT,
        CHECK (
            (kind = 'rotated' AND replacement_cred_id IS NOT NULL
                AND grace_until IS NOT NULL)
            OR (kind <> 'rotated' AND replacement_cred_id IS NULL
                AND grace_until IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS record_visibility_events (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        record_rowid INTEGER NOT NULL,
        event_kind TEXT NOT NULL CHECK (event_kind IN
            ('set_owner', 'scope_private', 'scope_shared', 'grant', 'revoke')),
        target_agent TEXT,
        actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
        target_event_seq INTEGER,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY (record_rowid) REFERENCES records_v1(id) ON DELETE RESTRICT,
        FOREIGN KEY (target_agent) REFERENCES agents(agent_id) ON DELETE RESTRICT,
        FOREIGN KEY (target_event_seq) REFERENCES record_visibility_events(event_seq),
        CHECK (
            (event_kind IN ('set_owner', 'grant') AND target_agent IS NOT NULL
                AND target_event_seq IS NULL)
            OR (event_kind = 'revoke' AND target_agent IS NOT NULL
                AND target_event_seq IS NOT NULL)
            OR (event_kind IN ('scope_private', 'scope_shared')
                AND target_agent IS NULL AND target_event_seq IS NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_visibility_events_record
    ON record_visibility_events (record_rowid, event_seq)
    """,
    """
    CREATE TABLE IF NOT EXISTS import_batches (
        batch_id TEXT PRIMARY KEY,
        package_sha256 TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        actor TEXT NOT NULL,
        owner_agent_id TEXT,
        grant_policy_json TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
        record_count INTEGER NOT NULL DEFAULT 0,
        membership_count INTEGER NOT NULL DEFAULT 0,
        grant_count INTEGER NOT NULL DEFAULT 0,
        summary_json TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY (owner_agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS config_kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # legacy 锚点：迁移一次性写入，之后由触发器锁死
    """
    INSERT INTO agents (agent_id, display_name, created_at)
    VALUES ('agt-legacy', 'Legacy principal (pre-v5 records)',
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    """,
    """
    INSERT INTO agent_events (agent_id, kind, actor, idempotency_key, created_at)
    VALUES ('agt-legacy', 'registered', 'migration-v5',
            'm5-v5-legacy-principal', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    """,
    """
    INSERT INTO config_kv (key, value, updated_at)
    VALUES ('legacy_principal', 'agt-legacy',
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    """,
    # 审计/事件/锚点表：只插不改不删
    """
    CREATE TRIGGER IF NOT EXISTS agents_immutable_update
    BEFORE UPDATE ON agents BEGIN
        SELECT RAISE(ABORT, 'agents is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agents_immutable_delete
    BEFORE DELETE ON agents BEGIN
        SELECT RAISE(ABORT, 'agents is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_events_immutable_update
    BEFORE UPDATE ON agent_events BEGIN
        SELECT RAISE(ABORT, 'agent_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_events_immutable_delete
    BEFORE DELETE ON agent_events BEGIN
        SELECT RAISE(ABORT, 'agent_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_credentials_immutable_update
    BEFORE UPDATE ON agent_credentials BEGIN
        SELECT RAISE(ABORT, 'agent_credentials is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_credentials_immutable_delete
    BEFORE DELETE ON agent_credentials BEGIN
        SELECT RAISE(ABORT, 'agent_credentials is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS credential_events_immutable_update
    BEFORE UPDATE ON credential_events BEGIN
        SELECT RAISE(ABORT, 'credential_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS credential_events_immutable_delete
    BEFORE DELETE ON credential_events BEGIN
        SELECT RAISE(ABORT, 'credential_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS record_visibility_events_immutable_update
    BEFORE UPDATE ON record_visibility_events BEGIN
        SELECT RAISE(ABORT, 'record_visibility_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS record_visibility_events_immutable_delete
    BEFORE DELETE ON record_visibility_events BEGIN
        SELECT RAISE(ABORT, 'record_visibility_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS config_kv_immutable_update
    BEFORE UPDATE ON config_kv BEGIN
        SELECT RAISE(ABORT, 'config_kv anchors are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS config_kv_immutable_delete
    BEFORE DELETE ON config_kv BEGIN
        SELECT RAISE(ABORT, 'config_kv anchors are immutable');
    END
    """,
)


# v6：投影留痕完整性。
#
# projection_claims 不是“完全不可变”：生成新版本时，旧 active 行必须发生
# 唯一合法的 active -> superseded 状态转移。除此之外，正文、来源哈希、规则、
# 版本身份等字段均不可原地改写，行也不可删除。projection_runs 与
# claim_evidence 只追加不改删。
MIGRATION_6_STATEMENTS = (
    """
    CREATE TRIGGER IF NOT EXISTS branch_memberships_immutable_update
    BEFORE UPDATE ON records_v1_branch_memberships BEGIN
        SELECT RAISE(ABORT, 'record branch membership is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS branch_memberships_immutable_delete
    BEFORE DELETE ON records_v1_branch_memberships BEGIN
        SELECT RAISE(ABORT, 'record branch membership is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS projection_runs_immutable_update
    BEFORE UPDATE ON projection_runs BEGIN
        SELECT RAISE(ABORT, 'projection_runs is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS projection_runs_immutable_delete
    BEFORE DELETE ON projection_runs BEGIN
        SELECT RAISE(ABORT, 'projection_runs is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS projection_claims_guard_update
    BEFORE UPDATE ON projection_claims
    WHEN NOT (
        NEW.id IS OLD.id
        AND NEW.claim_id IS OLD.claim_id
        AND NEW.claim_version IS OLD.claim_version
        AND NEW.agent_id IS OLD.agent_id
        AND NEW.claim_kind IS OLD.claim_kind
        AND NEW.content IS OLD.content
        AND OLD.status = 'active'
        AND NEW.status = 'superseded'
        AND NEW.rule_id IS OLD.rule_id
        AND NEW.rule_version IS OLD.rule_version
        AND NEW.source_hash IS OLD.source_hash
        AND NEW.projection_hash IS OLD.projection_hash
        AND NEW.run_id IS OLD.run_id
        AND NEW.created_at IS OLD.created_at
        AND OLD.superseded_by_version IS NULL
        AND NEW.superseded_by_version = OLD.claim_version + 1
    ) BEGIN
        SELECT RAISE(
            ABORT,
            'projection_claims permits only active-to-superseded transition'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS projection_claims_immutable_delete
    BEFORE DELETE ON projection_claims BEGIN
        SELECT RAISE(ABORT, 'projection_claims history cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS claim_evidence_immutable_update
    BEFORE UPDATE ON claim_evidence BEGIN
        SELECT RAISE(ABORT, 'claim_evidence is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS claim_evidence_immutable_delete
    BEFORE DELETE ON claim_evidence BEGIN
        SELECT RAISE(ABORT, 'claim_evidence is append-only');
    END
    """,
)


def _validate_projection_integrity_for_v6(conn: sqlite3.Connection) -> None:
    """拒绝在已不自洽的投影历史上安装 v6 机制化保护。"""
    invalid_status = conn.execute(
        "SELECT 1 FROM projection_claims "
        "WHERE (status = 'active' AND superseded_by_version IS NOT NULL) "
        "OR (status = 'superseded' AND superseded_by_version IS NULL) "
        "LIMIT 1"
    ).fetchone()
    if invalid_status is not None:
        raise sqlite3.IntegrityError(
            "projection history is inconsistent; v6 migration refused"
        )

    invalid_chain = conn.execute(
        "SELECT 1 FROM projection_claims old "
        "LEFT JOIN projection_claims newer "
        "ON newer.claim_id = old.claim_id "
        "AND newer.claim_version = old.superseded_by_version "
        "WHERE old.status = 'superseded' "
        "AND (old.superseded_by_version <> old.claim_version + 1 "
        "OR newer.id IS NULL) LIMIT 1"
    ).fetchone()
    if invalid_chain is not None:
        raise sqlite3.IntegrityError(
            "projection version chain is inconsistent; v6 migration refused"
        )

    orphan_link = conn.execute(
        "SELECT 1 FROM claim_evidence ce "
        "LEFT JOIN projection_claims pc ON pc.id = ce.claim_rowid "
        "LEFT JOIN records_v1 r ON r.id = ce.record_rowid "
        "WHERE pc.id IS NULL OR r.id IS NULL LIMIT 1"
    ).fetchone()
    if orphan_link is not None:
        raise sqlite3.IntegrityError(
            "claim evidence contains orphan links; v6 migration refused"
        )

    orphan_projection = conn.execute(
        "SELECT 1 FROM projection_claims pc "
        "LEFT JOIN projection_runs pr ON pr.run_id = pc.run_id "
        "WHERE pr.run_id IS NULL LIMIT 1"
    ).fetchone()
    if orphan_projection is not None:
        raise sqlite3.IntegrityError(
            "projection claims contain orphan runs; v6 migration refused"
        )

    orphan_membership = conn.execute(
        "SELECT 1 FROM records_v1_branch_memberships bm "
        "LEFT JOIN records_v1 r ON r.id = bm.record_rowid "
        "WHERE r.id IS NULL LIMIT 1"
    ).fetchone()
    if orphan_membership is not None:
        raise sqlite3.IntegrityError(
            "branch memberships contain orphan records; v6 migration refused"
        )

def migrate_records_db(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Apply all forward-only record migrations in one transaction."""

    conn = _connect(db_path)
    applied: List[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        migrations = (
            (1, "echo-pact-records-v1-with-fts5", MIGRATION_1_STATEMENTS),
            (2, "echo-pact-record-branch-memberships", MIGRATION_2_STATEMENTS),
            (3, "echo-pact-claims-projection-v1", MIGRATION_3_STATEMENTS),
            (4, "echo-pact-claim-conflicts-v1", MIGRATION_4_STATEMENTS),
            (5, "echo-pact-identity-visibility-v1", MIGRATION_5_STATEMENTS),
            (6, "echo-pact-projection-integrity-v1", MIGRATION_6_STATEMENTS),
        )
        for version, name, statements in migrations:
            exists = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (version,),
            ).fetchone()
            if exists:
                continue
            if version == 6:
                _validate_projection_integrity_for_v6(conn)
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (version, name, datetime.now(timezone.utc).isoformat()),
            )
            applied.append(version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"current_version": MIGRATION_VERSION, "applied": applied}


def _normalize_timestamp(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty ISO-8601 timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(record: Mapping[str, Any], field_name: str) -> str:
    value = record.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip() if field_name != "content" else value


def _normalize_branch_memberships(
    value: Any,
    *,
    legacy_branch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if legacy_branch_id is not None:
        return [{"branch_id": legacy_branch_id, "position": None}]
    if not isinstance(value, list) or not value:
        raise ValueError("branch_memberships must be a non-empty JSON array")
    memberships: Dict[str, Optional[int]] = {}
    for index, membership in enumerate(value, start=1):
        if not isinstance(membership, Mapping):
            raise ValueError(f"branch_memberships item {index} must be an object")
        branch_id = _required_text(membership, "branch_id")
        position = membership.get("position")
        if (
            not isinstance(position, int)
            or isinstance(position, bool)
            or position < 0
        ):
            raise ValueError(
                f"branch_memberships item {index} position must be a non-negative integer"
            )
        existing = memberships.get(branch_id)
        if existing is not None and existing != position:
            raise ValueError(
                f"branch_id {branch_id!r} has conflicting positions in one record"
            )
        memberships[branch_id] = position
    return [
        {"branch_id": branch_id, "position": memberships[branch_id]}
        for branch_id in sorted(memberships)
    ]


def validate_record(
    raw_record: Mapping[str, Any], *, inherited_schema_version: Optional[str] = None
) -> Dict[str, Any]:
    """Validate and normalize one V1 record without writing anything."""

    if not isinstance(raw_record, Mapping):
        raise ValueError("record must be a JSON object")
    schema_version = raw_record.get("schema_version", inherited_schema_version)
    if schema_version not in SUPPORTED_RECORD_SCHEMA_VERSIONS:
        raise ValueError(
            "schema_version must be one of "
            f"{sorted(SUPPORTED_RECORD_SCHEMA_VERSIONS)!r}, got {schema_version!r}"
        )
    required_fields = (
        REQUIRED_FIELDS
        if schema_version == RECORD_SCHEMA_VERSION
        else COMPACT_REQUIRED_FIELDS
    )
    missing = [field for field in required_fields if field not in raw_record]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    role = _required_text(raw_record, "role")
    if role not in ALLOWED_ROLES:
        raise ValueError(f"role must be one of {sorted(ALLOWED_ROLES)}")

    verified = raw_record.get("verified")
    if type(verified) is not bool:
        raise ValueError("verified must be a JSON boolean")

    conflict_group_id = raw_record.get("conflict_group_id")
    if conflict_group_id is not None:
        if not isinstance(conflict_group_id, str) or not conflict_group_id.strip():
            raise ValueError("conflict_group_id must be null or a non-empty string")
        conflict_group_id = conflict_group_id.strip()

    content = _required_text(raw_record, "content")
    if not content.strip():
        raise ValueError("content must not be blank")

    if schema_version == RECORD_SCHEMA_VERSION:
        branch_id = _required_text(raw_record, "branch_id")
        branch_memberships = _normalize_branch_memberships(
            None, legacy_branch_id=branch_id
        )
    else:
        branch_memberships = _normalize_branch_memberships(
            raw_record.get("branch_memberships")
        )
        branch_id = branch_memberships[0]["branch_id"]

    return {
        "schema_version": schema_version,
        "record_id": _required_text(raw_record, "record_id"),
        "source_kind": _required_text(raw_record, "source_kind"),
        "source_ref": _required_text(raw_record, "source_ref"),
        "conversation_id": _required_text(raw_record, "conversation_id"),
        "branch_id": branch_id,
        "branch_memberships": branch_memberships,
        "message_id": _required_text(raw_record, "message_id"),
        "role": role,
        "content": content,
        "created_at": _normalize_timestamp(raw_record.get("created_at"), "created_at"),
        "verified": verified,
        "authority": _required_text(raw_record, "authority"),
        "source_cutoff_at": _normalize_timestamp(
            raw_record.get("source_cutoff_at"), "source_cutoff_at"
        ),
        "conflict_group_id": conflict_group_id,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _merge_branch_memberships(
    existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    merged = {item["branch_id"]: item.get("position") for item in existing}
    for item in incoming:
        branch_id = item["branch_id"]
        position = item.get("position")
        previous = merged.get(branch_id)
        if previous is not None and position is not None and previous != position:
            raise RecordConflictError(
                f"branch membership position conflict for branch_id {branch_id!r}"
            )
        if previous is None:
            merged[branch_id] = position
    return [
        {"branch_id": branch_id, "position": merged[branch_id]}
        for branch_id in sorted(merged)
    ]


def _deduplicate_package(records: Iterable[Dict[str, Any]]) -> LoadedRecordPackage:
    unique: Dict[str, Dict[str, Any]] = {}
    duplicate_skips = 0
    for record in records:
        existing = unique.get(record["record_id"])
        if existing is None:
            unique[record["record_id"]] = record
        elif existing["content_sha256"] == record["content_sha256"]:
            existing["branch_memberships"] = _merge_branch_memberships(
                existing["branch_memberships"], record["branch_memberships"]
            )
            duplicate_skips += 1
        else:
            raise RecordConflictError(
                "record_id appears more than once with different content: "
                f"{record['record_id']}"
            )
    versions = {record["schema_version"] for record in unique.values()}
    schema_version = (
        versions.pop() if len(versions) == 1 else COMPACT_RECORD_SCHEMA_VERSION
    )
    return LoadedRecordPackage(list(unique.values()), duplicate_skips, schema_version)


def load_record_package(path: str) -> LoadedRecordPackage:
    """Load JSON or JSONL and validate the complete package before any write."""

    input_path = Path(path)
    if not input_path.is_file():
        raise RecordPackageError(f"Record package does not exist: {input_path}")
    try:
        raw_text = input_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RecordPackageError("Record package must be UTF-8") from exc
    if not raw_text.strip():
        raise RecordPackageError("Record package is empty")

    raw_records: List[Mapping[str, Any]]
    inherited_version: Optional[str] = None
    errors: List[str] = []

    if input_path.suffix.lower() == ".jsonl":
        raw_records = []
        for line_number, line in enumerate(raw_text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {line_number}: invalid JSON ({exc.msg})")
                continue
            if not isinstance(item, Mapping):
                errors.append(f"line {line_number}: record must be a JSON object")
                continue
            raw_records.append(item)
    else:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise RecordPackageError(f"Invalid JSON: {exc.msg}") from exc
        if isinstance(payload, Mapping) and "records" in payload:
            inherited_version = payload.get("schema_version")
            if not isinstance(payload["records"], list):
                raise RecordPackageError("records must be a JSON array")
            raw_records = payload["records"]
        elif isinstance(payload, list):
            raw_records = payload
        elif isinstance(payload, Mapping) and "record_id" in payload:
            raw_records = [payload]
        else:
            raise RecordPackageError(
                "JSON must be a V1 package object, an array, or one V1 record"
            )

    normalized: List[Dict[str, Any]] = []
    for index, raw_record in enumerate(raw_records, start=1):
        try:
            normalized.append(
                validate_record(
                    raw_record, inherited_schema_version=inherited_version
                )
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"record {index}: {exc}")

    if errors:
        raise RecordPackageError(
            "Record package validation failed: " + "; ".join(errors),
            summary={
                "schema_version": RECORD_SCHEMA_VERSION,
                "added": 0,
                "skipped": 0,
                "failed": len(errors),
                "errors": errors,
            },
        )
    if not normalized:
        raise RecordPackageError("Record package contains no records")
    return _deduplicate_package(normalized)


def _coverage_from_connection(
    conn: sqlite3.Connection,
    *,
    visible_rowids_query: Optional[str] = None,
    visibility_params: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Calculate coverage with the exact SQL visibility scope used by recall."""

    if visible_rowids_query is None:
        where = ""
        params: List[Any] = []
    else:
        where = f"WHERE id IN ({visible_rowids_query})"
        params = list(visibility_params or [])
    row = conn.execute(
        f"""
        SELECT
            MAX(CASE
                WHEN verified = 1 AND source_kind <> 'recent_patch'
                THEN source_cutoff_at
            END) AS verified_cutoff,
            MAX(created_at) AS latest_record_at,
            COUNT(*) AS visible_record_count
        FROM records_v1
        {where}
        """,
        params,
    ).fetchone()
    verified_cutoff = row["verified_cutoff"] if row else None
    latest_record_at = row["latest_record_at"] if row else None
    if visible_rowids_query is None:
        recent_where = ""
        recent_params: List[Any] = [verified_cutoff, verified_cutoff]
    else:
        recent_where = f"AND id IN ({visible_rowids_query})"
        recent_params = [verified_cutoff, verified_cutoff] + list(
            visibility_params or []
        )
    recent_row = conn.execute(
        f"""
        SELECT 1 FROM records_v1
        WHERE source_kind = 'recent_patch'
          AND verified = 0
          AND (? IS NULL OR created_at > ?)
          {recent_where}
        LIMIT 1
        """,
        recent_params,
    ).fetchone()
    result = {
        "verified_knowledge_cutoff_at": verified_cutoff,
        "latest_imported_record_at": latest_record_at,
        "contains_post_cutoff_unverified_recent_patch": bool(recent_row),
    }
    if visible_rowids_query is not None:
        result["_visible_record_count"] = (
            row["visible_record_count"] if row else 0
        )
    return result


def _package_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_record_package(
    path: str,
    *,
    db_path: Optional[str] = None,
    batch_size: int = 100,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    owner_agent_id: Optional[str] = None,
    actor: str = "internal",
    batch_id: Optional[str] = None,
    grant_policy: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Import a validated package in retry-safe transactional batches.

    Validation and conflict preflight happen before inserting any package
    record.  A process interruption can therefore leave only complete batches;
    rerunning the same package skips those record_ids and resumes the remainder.

    M5-04 批次绑定：每个批次在启动时写入 import_batches（running），绑定
    package_sha256/schema/actor/owner/grant_policy；同 batch_id 换输入冲突
    失败；相同输入可安全续跑；已完成批次重放直接返回既有 summary。
    owner_agent_id 显式给出时，每个分块事务同步写入 set_owner 事件
    （同事务提交，不存在"证据已写、授权未写"）。不给 owner（内部/旧测试
    路径）则不产生授权事件，记录按 legacy 兜底归属。
    """

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if grant_policy:
        raise ValueError(
            "grant_policy 尚未实现；请通过本地管理入口显式授权"
        )
    actor = actor.strip() if isinstance(actor, str) else ""
    if not actor:
        raise ValueError("actor must be a non-empty string")
    package_hash = _package_sha256(path)
    loaded = load_record_package(path)
    migrate_records_db(db_path)
    if batch_id is None:
        batch_id = "imp-" + hashlib.sha256(
            "|".join(
                [
                    package_hash,
                    loaded.schema_version,
                    actor,
                    owner_agent_id or "",
                    json.dumps(grant_policy or {}, sort_keys=True,
                               ensure_ascii=False),
                ]
            ).encode("utf-8")
        ).hexdigest()[:24]

    # ---- 批次登记 / 冲突 / 重放（独立事务先行） ----
    reg_conn = _connect(db_path)
    resumed_counts = {
        "record_count": 0,
        "membership_count": 0,
        "grant_count": 0,
    }
    try:
        reg_conn.execute("BEGIN IMMEDIATE")
        existing_batch = reg_conn.execute(
            "SELECT * FROM import_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        policy_json = json.dumps(
            grant_policy or {}, sort_keys=True, ensure_ascii=False
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        if existing_batch is not None:
            mismatches = []
            if existing_batch["package_sha256"] != package_hash:
                mismatches.append("package_sha256")
            if existing_batch["schema_version"] != loaded.schema_version:
                mismatches.append("schema_version")
            if existing_batch["actor"] != actor:
                mismatches.append("actor")
            if (existing_batch["owner_agent_id"] or None) != owner_agent_id:
                mismatches.append("owner_agent_id")
            if existing_batch["grant_policy_json"] != policy_json:
                mismatches.append("grant_policy")
            if mismatches:
                reg_conn.rollback()
                raise ValueError(
                    "batch_id 已绑定不同输入，拒绝执行: "
                    + ", ".join(mismatches)
                )
            if existing_batch["status"] == "completed":
                summary = dict(json.loads(existing_batch["summary_json"]))
                summary["batch_id"] = batch_id
                summary["idempotent_replay"] = True
                summary["added"] = 0
                summary["skipped"] = loaded.duplicate_skips + len(loaded.records)
                if loaded.schema_version == COMPACT_RECORD_SCHEMA_VERSION:
                    summary["branch_memberships_added"] = 0
                    summary["branch_memberships_skipped"] = sum(
                        len(record["branch_memberships"])
                        for record in loaded.records
                    )
                reg_conn.commit()
                return summary
            resumed_counts = {
                "record_count": existing_batch["record_count"],
                "membership_count": existing_batch["membership_count"],
                "grant_count": existing_batch["grant_count"],
            }
            # running/failed：相同输入，允许续跑；失败批次重置为 running
            if existing_batch["status"] == "failed":
                reg_conn.execute(
                    "UPDATE import_batches SET status = 'running' "
                    "WHERE batch_id = ?",
                    (batch_id,),
                )
            reg_conn.commit()
        else:
            if owner_agent_id is not None:
                owner_row = reg_conn.execute(
                    "SELECT agent_id FROM agents WHERE agent_id = ?",
                    (owner_agent_id,),
                ).fetchone()
                if owner_row is None:
                    reg_conn.rollback()
                    raise ValueError("owner_agent_id 必须是已注册的 agent")
                # 新批次登记前就拒掉停用 owner，不留 running 孤儿行
                from .identity import _agent_state
                if _agent_state(reg_conn, owner_agent_id) != "active":
                    reg_conn.rollback()
                    raise ValueError("owner_agent_id 必须是启用状态的 agent")
            reg_conn.execute(
                "INSERT INTO import_batches "
                "(batch_id, package_sha256, schema_version, actor, "
                " owner_agent_id, grant_policy_json, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
                (batch_id, package_hash, loaded.schema_version, actor,
                 owner_agent_id, policy_json, now_iso),
            )
            reg_conn.commit()
    except Exception:
        if reg_conn.in_transaction:
            reg_conn.rollback()
        raise
    finally:
        reg_conn.close()

    if owner_agent_id is not None:
        from .identity import _require_agent_active
        # sqlite3.Connection 的 context manager 只提交/回滚，不负责 close；
        # 彩排使用临时数据库，句柄必须显式关闭才能在 Windows 上安全清理。
        with closing(_connect(db_path)) as check_conn:
            check_conn.execute("PRAGMA query_only = ON")
            _require_agent_active(check_conn, owner_agent_id)

    conn = _connect(db_path)
    try:
        existing: Dict[str, Dict[str, Any]] = {}
        record_ids = [record["record_id"] for record in loaded.records]
        for start in range(0, len(record_ids), 500):
            chunk = record_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT id, record_id, content_sha256 FROM records_v1 "
                f"WHERE record_id IN ({placeholders})",
                chunk,
            ).fetchall():
                existing[row["record_id"]] = {
                    "rowid": row["id"],
                    "content_sha256": row["content_sha256"],
                    "memberships": {},
                }

        existing_items = list(existing.items())
        for start in range(0, len(existing_items), 500):
            chunk = existing_items[start : start + 500]
            rowid_to_record_id = {item[1]["rowid"]: item[0] for item in chunk}
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT record_rowid, branch_id, position "
                "FROM records_v1_branch_memberships "
                f"WHERE record_rowid IN ({placeholders})",
                list(rowid_to_record_id),
            ).fetchall():
                existing[rowid_to_record_id[row["record_rowid"]]]["memberships"][
                    row["branch_id"]
                ] = row["position"]

        conflicts = [
            record["record_id"]
            for record in loaded.records
            if record["record_id"] in existing
            and existing[record["record_id"]]["content_sha256"]
            != record["content_sha256"]
        ]
        membership_conflicts: List[str] = []
        for record in loaded.records:
            current = existing.get(record["record_id"])
            if current is None:
                continue
            for membership in record["branch_memberships"]:
                branch_id = membership["branch_id"]
                incoming_position = membership["position"]
                if branch_id not in current["memberships"]:
                    continue
                stored_position = current["memberships"][branch_id]
                if (
                    stored_position is not None
                    and incoming_position is not None
                    and stored_position != incoming_position
                ):
                    membership_conflicts.append(
                        f"{record['record_id']}@{branch_id}"
                    )
        conflicts.extend(membership_conflicts)
        if conflicts:
            raise RecordConflictError(
                "record content or branch membership conflict; existing data was not overwritten: "
                + ", ".join(conflicts),
                summary={
                    "schema_version": loaded.schema_version,
                    "added": 0,
                    "skipped": 0,
                    "failed": len(conflicts),
                    "conflicts": conflicts,
                },
            )

        work_records = []
        membership_skips = 0
        for record in loaded.records:
            current = existing.get(record["record_id"])
            if current is None:
                work_records.append(record)
                continue
            missing_memberships = [
                membership
                for membership in record["branch_memberships"]
                if membership["branch_id"] not in current["memberships"]
            ]
            membership_skips += len(record["branch_memberships"]) - len(
                missing_memberships
            )
            if missing_memberships:
                work_record = dict(record)
                work_record["branch_memberships"] = missing_memberships
                work_records.append(work_record)
        summary: Dict[str, Any] = {
            "schema_version": loaded.schema_version,
            "added": 0,
            "skipped": loaded.duplicate_skips + len(existing),
            "failed": 0,
            "knowledge_cutoff_at": None,
            "latest_record_at": None,
        }
        if loaded.schema_version == COMPACT_RECORD_SCHEMA_VERSION:
            summary.update(
                {
                    "branch_memberships_added": 0,
                    "branch_memberships_skipped": membership_skips,
                }
            )
        inserted_at = datetime.now(timezone.utc).isoformat()
        insert_sql = """
            INSERT INTO records_v1 (
                schema_version, record_id, source_kind, source_ref,
                conversation_id, branch_id, message_id, role, content,
                created_at, verified, authority, source_cutoff_at,
                conflict_group_id, content_sha256, imported_at
            ) VALUES (
                :schema_version, :record_id, :source_kind, :source_ref,
                :conversation_id, :branch_id, :message_id, :role, :content,
                :created_at, :verified, :authority, :source_cutoff_at,
                :conflict_group_id, :content_sha256, :imported_at
            )
        """

        membership_sql = """
            INSERT INTO records_v1_branch_memberships(
                record_rowid, branch_id, position
            ) VALUES (?, ?, ?)
        """

        for start in range(0, len(work_records), batch_size):
            batch = work_records[start : start + batch_size]
            batch_added = 0
            batch_memberships_added = 0
            newly_owned: List[str] = []
            try:
                conn.execute("BEGIN IMMEDIATE")
                membership_values = []
                for record in batch:
                    current = existing.get(record["record_id"])
                    if current is None:
                        values = dict(record)
                        values["verified"] = int(record["verified"])
                        values["imported_at"] = inserted_at
                        cursor = conn.execute(insert_sql, values)
                        record_rowid = cursor.lastrowid
                        existing[record["record_id"]] = {
                            "rowid": record_rowid,
                            "content_sha256": record["content_sha256"],
                            "memberships": {},
                        }
                        batch_added += 1
                        newly_owned.append(record["record_id"])
                    else:
                        record_rowid = current["rowid"]
                    for membership in record["branch_memberships"]:
                        membership_values.append(
                            (
                                record_rowid,
                                membership["branch_id"],
                                membership["position"],
                            )
                        )
                        existing[record["record_id"]]["memberships"][
                            membership["branch_id"]
                        ] = membership["position"]
                        batch_memberships_added += 1
                # M5-04：owner 授权事件与证据同事务提交
                if owner_agent_id is not None and batch_added:
                    from .identity import _emit_event

                    for record in batch:
                        if record["record_id"] in newly_owned:
                            _emit_event(
                                conn,
                                existing[record["record_id"]]["rowid"],
                                "set_owner",
                                target_agent=owner_agent_id,
                                actor=actor,
                                target_event_seq=None,
                                now=inserted_at,
                                idem_parts=[batch_id, record["record_id"],
                                            owner_agent_id],
                            )
                    conn.execute(
                        "UPDATE import_batches SET record_count = ?, "
                        "membership_count = ?, grant_count = grant_count + ? "
                        "WHERE batch_id = ?",
                        (resumed_counts["record_count"]
                         + summary["added"] + batch_added,
                         resumed_counts["membership_count"]
                         + summary.get("branch_memberships_added", 0)
                         + batch_memberships_added,
                         resumed_counts["grant_count"]
                         + summary["added"] + batch_added, batch_id),
                    )
                else:
                    conn.execute(
                        "UPDATE import_batches SET record_count = ?, "
                        "membership_count = ? WHERE batch_id = ?",
                        (resumed_counts["record_count"]
                         + summary["added"] + batch_added,
                         resumed_counts["membership_count"]
                         + summary.get("branch_memberships_added", 0)
                         + batch_memberships_added, batch_id),
                    )
                conn.executemany(membership_sql, membership_values)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            summary["added"] += batch_added
            if loaded.schema_version == COMPACT_RECORD_SCHEMA_VERSION:
                summary["branch_memberships_added"] += batch_memberships_added
            coverage = _coverage_from_connection(conn)
            summary["knowledge_cutoff_at"] = coverage[
                "verified_knowledge_cutoff_at"
            ]
            summary["latest_record_at"] = coverage["latest_imported_record_at"]
            if progress_callback:
                progress_callback(dict(summary))

        if not work_records:
            coverage = _coverage_from_connection(conn)
            summary["knowledge_cutoff_at"] = coverage[
                "verified_knowledge_cutoff_at"
            ]
            summary["latest_record_at"] = coverage["latest_imported_record_at"]
        summary["batch_id"] = batch_id
        summary["idempotent_replay"] = False
        fin = _connect(db_path)
        try:
            fin.execute("BEGIN IMMEDIATE")
            fin.execute(
                "UPDATE import_batches SET status = 'completed', "
                "summary_json = ?, "
                "completed_at = ? WHERE batch_id = ?",
                (json.dumps(summary, sort_keys=True, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(), batch_id),
            )
            fin.commit()
        except Exception:
            if fin.in_transaction:
                fin.rollback()
            raise
        finally:
            fin.close()
        return summary
    except Exception:
        # 分块阶段任意失败：持久化 failed 状态（批次行此时必存在且为
        # running；登记阶段的异常在上方独立事务里已处理，不会走到这里）。
        # 失败批次的唯一恢复路径是按裁定 2 重放相同输入续跑。
        fail_conn = _connect(db_path)
        try:
            fail_conn.execute(
                "UPDATE import_batches SET status = 'failed' "
                "WHERE batch_id = ? AND status = 'running'",
                (batch_id,),
            )
            fail_conn.commit()
        finally:
            fail_conn.close()
        raise
    finally:
        conn.close()


def check_records_index_consistency(
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare immutable records, durable index state, and FTS5 doc rows."""

    conn = _connect(db_path)
    try:
        records = {
            row["id"]: row["content_sha256"]
            for row in conn.execute(
                "SELECT id, content_sha256 FROM records_v1"
            ).fetchall()
        }
        states = {
            row["record_rowid"]: row["content_sha256"]
            for row in conn.execute(
                "SELECT record_rowid, content_sha256 FROM records_v1_index_state"
            ).fetchall()
        }
        fts_rowids = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM records_v1_fts_docsize"
            ).fetchall()
        }
        membership_rowids = {
            row["record_rowid"]
            for row in conn.execute(
                "SELECT DISTINCT record_rowid FROM records_v1_branch_memberships"
            ).fetchall()
        }
        missing_state = sorted(set(records) - set(states))
        orphan_state = sorted(set(states) - set(records))
        stale_state = sorted(
            rowid
            for rowid in set(records) & set(states)
            if records[rowid] != states[rowid]
        )
        missing_fts = sorted(set(records) - fts_rowids)
        orphan_fts = sorted(fts_rowids - set(records))
        missing_memberships = sorted(set(records) - membership_rowids)
        orphan_memberships = sorted(membership_rowids - set(records))
        ok = not any(
            (
                missing_state,
                orphan_state,
                stale_state,
                missing_fts,
                orphan_fts,
                missing_memberships,
                orphan_memberships,
            )
        )
        return {
            "ok": ok,
            "records": len(records),
            "index_state": len(states),
            "fts_documents": len(fts_rowids),
            "missing_state": missing_state,
            "orphan_state": orphan_state,
            "stale_state": stale_state,
            "missing_fts": missing_fts,
            "orphan_fts": orphan_fts,
            "branch_membership_records": len(membership_rowids),
            "missing_memberships": missing_memberships,
            "orphan_memberships": orphan_memberships,
        }
    finally:
        conn.close()


def rebuild_records_index(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Repeatably rebuild the derived FTS/index state from source records."""

    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO records_v1_fts(records_v1_fts) VALUES ('rebuild')"
        )
        conn.execute("DELETE FROM records_v1_index_state")
        conn.execute(
            """
            INSERT INTO records_v1_index_state(
                record_rowid, content_sha256, indexed_at
            )
            SELECT id, content_sha256, ? FROM records_v1
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return check_records_index_consistency(db_path)


@dataclass(frozen=True)
class DirectionalRelationIntent:
    """Role-bound relationship direction expressed by the query text.

    ``query_speaker_role`` describes the speaker of the natural-language
    question, not a private person or a configured agent.  Historical ``user``
    rows keep that perspective; historical ``assistant`` rows reverse the
    surface pronouns while preserving the same semantic roles.
    """

    relation: str
    query_speaker_role: str


@dataclass(frozen=True)
class RecallQueryPlan:
    """Deterministic, dependency-free tiers for SQLite lexical recall."""

    normalized_query: str
    exact_expressions: Sequence[str]
    focused_expression: Optional[str]
    focused_relaxed_expression: Optional[str]
    focused_like_terms: Sequence[str]
    relaxed_expression: Optional[str]
    like_terms: Sequence[str]
    prefer_oldest: bool
    prefer_latest: bool
    intent_required_all_like_terms: Sequence[str]
    intent_required_any_like_terms: Sequence[str]
    intent_bonus_like_terms: Sequence[str]
    intent_prefer_compact: bool
    intent_min_any_matches: int
    intent_roles: Sequence[str]
    intent_relevance_first: bool
    explicit_literal_anchors: Sequence[str]
    shared_event_intent: bool
    shared_event_fts_terms: Sequence[str]
    shared_event_like_terms: Sequence[str]
    shared_event_anchor_fts_terms: Sequence[str]
    shared_event_anchor_like_terms: Sequence[str]
    directional_relation: Optional[DirectionalRelationIntent]


@dataclass(frozen=True)
class SharedEventMatch:
    """One bounded same-branch window supporting a shared event candidate."""

    representative_rowid: int
    event_start_at: str
    event_end_at: str
    event_evidence: Sequence[Mapping[str, Any]]
    participation_evidence: Sequence[Mapping[str, Any]]
    support_status: str
    support_reason: str
    assistant_identity_status: str
    signature: Sequence[str]


MAX_RELAXED_TERMS_PER_GROUP = 24
MAX_LIKE_TERMS = 16
_CJK_QUESTION_SHELLS = (
    "你是从什么时候开始喜欢",
    "你是从什么时候开始",
    "你还记不记得",
    "你还记得起",
    "还记不记得",
    "还记得起",
    "从什么时候开始",
    "你是什么时候",
    "是什么时候",
    "在我们这里",
    "有什么意思",
    "是什么意思",
    "你还记得",
    "还记得",
    "你送我的",
    "你陪我在",
    "你说过",
    "那时候",
    "的时候",
    "叫什么名字",
    "请告诉我",
    "我想知道",
    "记不记得",
    "有没有",
    "能不能",
    "会不会",
    "怎么样",
    "为什么",
    "是什么",
    "叫什么",
    "哪一个",
    "告诉我",
    "请问",
    "怎么",
    "哪里",
    "哪个",
    "多少",
    "是谁",
    "什么",
    "以后",
)

_CJK_QUERY_ALIASES = (
    # A common speech-to-text substitution in the real acceptance questions.
    # Keep aliases narrow and evidence-neutral: this repairs spelling only and
    # does not inject an answer or a private-domain synonym.
    ("检校培训", "警校培训"),
)

_EARLIEST_QUERY_MARKERS = (
    "从什么时候开始",
    "什么时候开始",
    "第一次",
    "第一件",
    "第一个",
    "第一份",
    "第一样",
    "最早",
    "最初",
)

_MEANING_QUERY_MARKERS = (
    "有什么意思",
    "是什么意思",
    "什么含义",
    "代表什么",
    "象征什么",
)

_MEANING_CONTEXT_TERMS = (
    "故事",
    "含义",
    "意思",
    "象征",
    "代表",
    "比喻",
    "指代",
)

_GIFT_QUERY_MARKERS = ("礼物", "赠礼", "礼品")
_GIFT_RELATION_TERMS = (
    "礼物",
    "赠礼",
    "礼品",
    "送给",
    "送你",
    "送出",
    "送的",
    "给你",
    "收到",
    "第一件",
    "第一个",
    "第一份",
    "第一次",
    "最早",
    "最初",
)
_GIFT_EARLIEST_BONUS_TERMS = (
    "第一件",
    "第一个",
    "第一份",
    "第一次",
    "最早",
    "最初",
)

_PREFERENCE_QUERY_MARKERS = ("最喜欢", "最爱", "偏爱", "更喜欢")
_PREFERENCE_CONTEXT_TERMS = (
    "最喜欢",
    "最爱",
    "偏爱",
    "更喜欢",
    "喜欢",
    "钟意",
    "中意",
    "会选",
    "选择",
)
_PREFERENCE_BONUS_TERMS = (
    "我最喜欢",
    "我最爱",
    "我偏爱",
    "我更喜欢",
    "我会选",
    "我的选择",
    "回答",
)

_LATEST_QUERY_MARKERS = (
    "最近一次",
    "最近那次",
    "前几天那次",
    "上一次",
    "上一回",
    "上次",
    "前一次",
)
_LATEST_EVENT_PREFIX_MARKERS = (
    "前几天那次",
    "最近一次",
    "最近那次",
    "上一次",
    "上一回",
    "前一次",
    "上次",
    "我跟你一起",
    "我和你一起",
    "你跟我一起",
    "你和我一起",
    "我们一起",
    "咱们一起",
    "我们",
    "咱们",
    "那一家",
    "这一家",
    "那一次",
    "这一次",
    "那家",
    "这家",
    "那次",
    "这次",
    "那个",
    "这个",
    "点了",
    "叫了",
    "订了",
    "买了",
    "去了",
    "点",
    "叫",
    "订",
    "买",
    "去",
)
_LATEST_EVENT_SUFFIX_MARKERS = (
    "的时候",
    "那一次",
    "那一回",
    "那次",
    "当时",
    "吃过",
    "喝过",
    "吃了",
    "喝了",
    "吃",
    "喝",
)
_LATEST_EVENT_NON_TOPIC_TERMS = {
    "",
    "之前",
    "后来",
    "当时",
    "当时是",
    "当时说",
    "喜欢",
    "喜不喜欢",
    "觉得",
    "评价",
    "回答",
    "怎么说",
    "说",
    "怎样",
    "怎么样",
    "如何",
    "好不好",
    "好吃",
}
_LATEST_EVENT_BONUS_TERMS = (
    "喜欢",
    "好吃",
    "味道",
    "口味",
    "评价",
    "满意",
    "觉得",
    "回答",
    "当时说",
)

_ORIGINAL_EVIDENCE_QUERY_MARKERS = (
    "只根据原始消息",
    "只看原始消息",
    "原始消息",
    "原始记录",
    "原话",
    "原文",
    "逐字",
    "当时说了什么",
    "当时说的什么",
    "说了什么话",
    "说的是什么话",
    "亲口说",
    "怎么说的",
)

_RETELLING_QUERY_MARKERS = (
    "后来怎么复盘",
    "后来怎样复盘",
    "后来怎么回忆",
    "后来怎样回忆",
    "后来怎么整理",
    "后来怎样整理",
)

_ORIGINAL_TRACE_META_MARKERS = (
    "后来我专门复盘",
    "后来复盘",
    "后来回忆",
    "后来整理",
    "资料汇总",
    "经过整理",
    "用于归档",
    "总结",
    "关键词",
    "原话是",
    "原文是",
)

MAX_ORIGINAL_TRACE_ANCHORS = 8
MAX_ORIGINAL_TRACE_ANCHOR_CHARS = 120

RECALL_CONTEXT_BEFORE = 1
RECALL_CONTEXT_AFTER = 2
MAX_RECALL_CONTEXT_RECORDS = 4
RECALL_LITERAL_CONTEXT_BEFORE = 2
RECALL_LITERAL_CONTEXT_AFTER = 4
MAX_RECALL_LITERAL_CONTEXT_RECORDS = 6

MAX_SHARED_EVENT_CANDIDATES = 200
MAX_SHARED_EVENT_RESULTS = 5
SHARED_EVENT_CONTEXT_BEFORE = 6
SHARED_EVENT_CONTEXT_AFTER = 4
MAX_SHARED_EVENT_CONTEXT_RECORDS = (
    SHARED_EVENT_CONTEXT_BEFORE + SHARED_EVENT_CONTEXT_AFTER + 1
)

_SHARED_EVENT_QUERY_MARKERS = (
    "一起",
    "共同",
    "我们俩",
    "我们两",
    "我们两个",
    "两个人都",
)

_SHARED_EVENT_DYADIC_MARKERS = (
    "我跟你",
    "我和你",
    "你跟我",
    "你和我",
    "我们俩",
    "我们两个人",
    "我们两个",
    "两个人都",
    "咱俩",
    "你我",
)

_SHARED_EVENT_TOPIC_STRIP_MARKERS = (
    "我跟你一起",
    "我和你一起",
    "你跟我一起",
    "你和我一起",
    "我们两个一起",
    "我们两个人一起",
    "我们俩一起",
    "两个人都",
    "我们两个",
    "我们两个人",
    "我们俩",
    "跟我一起",
    "和我一起",
    "跟你一起",
    "和你一起",
    "我们一起",
    "共同",
    "咱们",
    "我们",
    "一起",
)

_SHARED_EVENT_UNANSWERABLE_TOPICS = {
    "",
    "做",
    "做了",
    "事情",
    "那件事",
    "那个事",
    "发生",
    "发生了",
}

_SHARED_EVENT_CREATIVE_MARKERS = (
    "故事里",
    "小说里",
    "画面",
    "场景",
    "漫画",
    "短视频",
    "画面里",
    "画面中",
    "提示词",
    "想象中",
    "梦里",
    "角色扮演",
    "设定中",
    "虚构",
)

_SHARED_EVENT_CREATIVE_TOPIC_MARKERS = (
    "画",
    "绘",
    "图",
    "写",
    "创作",
    "故事",
    "小说",
    "视频",
)

_SHARED_EVENT_FUTURE_RE = re.compile(
    r"(?:以后|下次|改天|将来|有机会|哪天).{0,18}"
    r"(?:一起|共同|我跟你|我和你|你跟我|你和我|我们)"
)
_SHARED_EVENT_HYPOTHETICAL_RE = re.compile(
    r"(?:如果|假如|要是|倘若).{0,30}"
    r"(?:一起|共同|我跟你|我和你|你跟我|你和我|我们)"
)
_SHARED_EVENT_NEGATIVE_RE = re.compile(
    r"(?:没有|没|从没|从未|不曾|还没|并没).{0,12}"
    r"(?:一起|共同|我跟你|我和你|你跟我|你和我|我们)"
    r"|(?:一起|共同|我跟你|我和你|你跟我|你和我|我们).{0,12}"
    r"(?:没有|没|从没|从未|不曾|还没|并没)"
)
_SHARED_EVENT_INVITATION_RE = re.compile(
    r"(?:要不要|想不想|愿不愿意).{0,18}(?:一起|共同)"
)
_SHARED_EVENT_COMPANION_ONLY_RE = re.compile(
    r"(?:陪|陪着|看着)(?:你|我).{0,4}(?:吃|喝)"
)

_SHARED_EVENT_GENERIC_FOOD_QUERY_MARKERS = (
    "吃东西",
    "吃饭",
    "吃的",
    "吃什么",
    "吃了什么",
)

# A generic meal question must not treat every literal ``一起吃...`` phrase as
# food.  These are source-neutral category signals, not private answer aliases:
# they keep ingestion, idioms, and smoking from masquerading as a shared meal.
_SHARED_EVENT_FOOD_CONTEXT_MARKERS = (
    "食物",
    "食品",
    "饭菜",
    "菜品",
    "吃饭",
    "早餐",
    "早饭",
    "午餐",
    "午饭",
    "晚餐",
    "晚饭",
    "夜宵",
    "烧烤",
    "火锅",
    "外卖",
    "零食",
    "小吃",
    "甜品",
    "甜点",
    "米饭",
    "米线",
    "面条",
    "面包",
    "饺子",
    "包子",
    "馒头",
    "蛋糕",
    "牛肉",
    "羊肉",
    "鸡肉",
    "猪肉",
    "鱼肉",
    "海鲜",
    "鸡翅",
    "口蘑",
    "蘑菇",
    "蔬菜",
    "青菜",
    "清淡",
    "辣的",
    "甜的",
    "咸的",
    "水果",
    "苹果",
    "香蕉",
    "草莓",
    "西瓜",
    "火腿",
    "香肠",
    "鸡蛋",
    "辣椒",
    "蘸料",
    "饮料",
    "奶茶",
    "咖啡",
)

_SHARED_EVENT_NON_FOOD_CONSUMPTION_RE = re.compile(
    r"(?:一起|共同|我跟你|我和你|你跟我|你和我|我们).{0,6}"
    r"吃(?:着|了|过)?(?:电子烟|香烟|烟|药|药片|胶囊|亏|苦头|哑巴亏)"
)


def _unique_text(values: Iterable[str]) -> List[str]:
    seen = set()
    unique = []
    for value in values:
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _bounded_text(values: Sequence[str], limit: int) -> List[str]:
    """Keep deterministic coverage across a long query without SQL explosion."""

    if len(values) <= limit:
        return list(values)
    if limit == 1:
        return [values[0]]
    indexes = [
        round(index * (len(values) - 1) / (limit - 1))
        for index in range(limit)
    ]
    return [values[index] for index in indexes]


def _wants_original_evidence(normalized_query: str) -> bool:
    """Return whether one bounded source-tracing pass is justified.

    The decision is based only on evidence-type wording in the user's query,
    never on a private topic such as a proposal, gift or relationship event.
    A question explicitly asking for a later recap keeps the recap relevant.
    """

    if any(marker in normalized_query for marker in _RETELLING_QUERY_MARKERS):
        return False
    return any(
        marker in normalized_query for marker in _ORIGINAL_EVIDENCE_QUERY_MARKERS
    )


def _original_trace_anchors(rows: Sequence[sqlite3.Row]) -> List[str]:
    """Extract bounded verbatim anchors from first-pass evidence.

    Later retellings often preserve one or two distinctive sentences from the
    original event even when the original does not repeat the question's topic
    words. We extract only literal text already present in returned evidence;
    no model, network call, entity guess or answer-specific synonym is used.
    """

    ranked: List[tuple[int, int, str]] = []
    for row in rows:
        content = str(row["content"] or "")
        quoted = [
            match.group(1) or match.group(2)
            for match in re.finditer(
                r"[“「『]([^”」』]{8,120})[”」』]|\"([^\"\r\n]{8,120})\"",
                content,
            )
        ]
        fragments = quoted + content.splitlines()
        quoted_values = set(quoted)
        for raw_fragment in fragments:
            fragment = re.sub(
                r"^\s*(?:[-*•>]+|\d+[.)、])\s*", "", raw_fragment
            ).strip(" \t'\"“”「」『』")
            fragment = re.sub(r"\s+", " ", fragment).strip()
            if not 8 <= len(fragment) <= MAX_ORIGINAL_TRACE_ANCHOR_CHARS:
                continue
            if fragment.endswith(("：", ":")):
                continue
            if any(marker in fragment for marker in _ORIGINAL_TRACE_META_MARKERS):
                continue
            lexical_chars = re.findall(r"[0-9A-Za-z\u3400-\u9fff]", fragment)
            if len(lexical_chars) < 8:
                continue
            # Quoted text is the strongest signal. Otherwise prefer longer
            # distinctive lines while keeping the number of SQL terms bounded.
            ranked.append(
                (1 if fragment in quoted_values else 0, len(fragment), fragment)
            )

    ordered = sorted(ranked, key=lambda item: (-item[0], -item[1], item[2]))
    return _bounded_text(
        _unique_text(item[2] for item in ordered), MAX_ORIGINAL_TRACE_ANCHORS
    )


def _quote_fts(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _fts_expression(query: str) -> Optional[str]:
    """Return the precision-first exact phrase used by the legacy fast path."""

    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", query)).strip()
    return _quote_fts(normalized) if len(normalized) >= 3 else None


def _focused_query_segments(normalized: str) -> List[str]:
    """Remove conversational scaffolding while preserving evidence anchors.

    Chinese questions often arrive as one unsegmented run.  Whole-question FTS
    is intentionally tried first, but when it misses we strip only a bounded
    list of address/question phrases, split clauses, and retain the remaining
    lexical material.  This is not semantic answer generation: no dates,
    entities, or private facts are added to the query.
    """

    if not re.search(r"[\u3400-\u9fff]", normalized):
        return []
    focused = normalized
    for source, target in _CJK_QUERY_ALIASES:
        focused = focused.replace(source, target)

    clauses = re.split(r"[，,。！？!?；;：:]", focused)
    segments: List[str] = []
    for clause in clauses:
        clause = re.sub(r"^(?:老公|老婆|宝贝|大宝贝)\s*", "", clause)
        for shell in _CJK_QUESTION_SHELLS:
            clause = clause.replace(shell, " ")
        # Keep first-person scope (especially ``我们``) intact.  Removing one
        # leading character from ``我们`` previously turned it into ``们`` and
        # allowed third-party or fictional events to outrank shared history.
        clause = re.sub(r"^(?:你|他|她|它)(?!们)\s*", "", clause)
        clause = re.sub(r"[啊呀呢嘛吗嘞哦]+$", "", clause)
        clause = clause.replace("的", " ").replace("很", " ")
        segments.extend(
            re.findall(
                r"[0-9A-Za-z]+(?:[-_][0-9A-Za-z]+)*|[\u3400-\u9fff]+",
                clause,
            )
        )
    return _unique_text(segments)


def _explicit_query_anchors(normalized: str) -> List[str]:
    """Return only literal anchors the caller explicitly supplied.

    Quoted wording and artifact titles are deliberate evidence handles.  When
    there is no quoted handle, a bounded ASCII name/code can serve the same
    purpose (for example a product name).  Numeric room/archive labels are
    retained alongside quoted text because they often disambiguate a later
    explanation from the original message.  No synonym or private fact is
    introduced here.
    """

    anchors: List[str] = []
    for pattern in (
        r"“([^”]{1,80})”",
        r'"([^"\r\n]{1,80})"',
        r"《([^》]{1,80})》",
    ):
        for match in re.finditer(pattern, normalized):
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if value:
                anchors.append(value)

    if not anchors:
        anchors.extend(
            re.findall(r"(?<![0-9A-Za-z_])[A-Za-z][0-9A-Za-z_-]{2,}", normalized)
        )

    anchors.extend(
        re.findall(
            r"\d{1,4}(?:号房|万多条记忆|万条记忆|条记忆)", normalized
        )
    )
    return _bounded_text(_unique_text(anchors), 3)


def _explicit_query_bonus_terms(
    normalized: str, required_anchors: Sequence[str]
) -> List[str]:
    """Return bounded literal terms from an explicit enumeration.

    A list such as ``glass house、cliff、blue key、song title`` describes one
    scene even when no single message contains every term.  The strongest
    quoted/title handle remains required; the remaining list items only rank
    candidate rows and never become invented synonyms or hard filters.
    """

    if "、" not in normalized:
        return []
    required = {value.casefold() for value in required_anchors}
    terms: List[str] = []
    for raw in normalized.split("、"):
        value = raw.strip().strip("，,。！？!?；;：:‘’'\"“”《》()（）[]【】")
        if not value or value.casefold() in required:
            continue
        if not 2 <= len(value) <= 16:
            continue
        if not re.fullmatch(r"[0-9A-Za-z\u3400-\u9fff _-]+", value):
            continue
        terms.append(re.sub(r"\s+", " ", value).strip())
    return _bounded_text(_unique_text(terms), MAX_LIKE_TERMS)


def _temporal_topic_terms(segment: str) -> List[str]:
    """Return bounded literal anchors for an earliest/first-event question."""

    topic = segment
    for marker in (
        "我们",
        "第一次",
        "第一回",
        "第一件",
        "第一个",
        "第一份",
        "第一样",
        "最早",
        "最初",
        "开始",
    ):
        topic = topic.replace(marker, "")
    topic = topic.strip()
    if not topic:
        return []
    variants = [topic]
    if "我" in topic or "你" in topic:
        # A remembered exchange naturally reverses speaker perspective:
        # "我给你..." in one turn may be "你给我..." in the reply.
        variants.append(topic.replace("我", "\0").replace("你", "我").replace("\0", "你"))
    terms: List[str] = []
    for variant in variants:
        if not re.fullmatch(r"[\u3400-\u9fff]+", variant) or len(variant) <= 4:
            terms.append(variant)
        else:
            terms.extend(
                variant[index : index + 4]
                for index in range(len(variant) - 3)
            )
    return _unique_text(terms)


def _preference_query_entity(normalized: str) -> Optional[str]:
    """Extract the literal subject of a preference question, if explicit.

    This deliberately returns only text already present next to a generic
    preference marker.  It never supplies a domain synonym or an expected
    answer, so the same rule works for perfume, food, music, tools, and future
    source adapters.
    """

    if not any(marker in normalized for marker in _PREFERENCE_QUERY_MARKERS):
        return None
    compact = re.sub(r"\s+", "", normalized)
    patterns = (
        r"(?:最喜欢|最爱|偏爱|更喜欢)(?:的)?"
        r"(?P<entity>[0-9A-Za-z\u3400-\u9fff_-]{2,16}?)"
        r"(?:是(?:哪|什么)|哪(?:一种|一款|个|款|类)|什么|"
        r"[，,。！？!?；;：:]|$)",
        r"(?:最喜欢|最爱|偏爱|更喜欢)"
        r"(?:哪一种|哪一款|哪一个|哪款|哪类|什么)"
        r"(?P<entity>[0-9A-Za-z\u3400-\u9fff_-]{2,16}?)"
        r"(?:[，,。！？!?；;：:]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            entity = match.group("entity").strip("的")
            if len(entity) >= 2:
                return entity
    return None


def _latest_composite_topic_terms(normalized: str) -> List[str]:
    """Keep multiple literal subjects from a latest-event question.

    The legacy planner intentionally selected one longest clause.  That is a
    good precision default, but it drops the second half of questions such as
    "the takeaway barbecue last time".  Here we conservatively strip only
    conversational/time/action glue and require at least two surviving literal
    anchors before activating the composite-event tier.
    """

    if not any(marker in normalized for marker in _LATEST_QUERY_MARKERS):
        return []

    topics: List[str] = []
    for raw_segment in _focused_query_segments(normalized):
        topic = raw_segment.strip()
        changed = True
        while topic and changed:
            changed = False
            for marker in _LATEST_EVENT_PREFIX_MARKERS:
                if topic.startswith(marker):
                    topic = topic[len(marker) :].strip()
                    changed = True
                    break
        changed = True
        while topic and changed:
            changed = False
            for marker in _LATEST_EVENT_SUFFIX_MARKERS:
                if topic.endswith(marker):
                    topic = topic[: -len(marker)].strip()
                    changed = True
                    break
        topic = topic.strip("的")
        if (
            topic in _LATEST_EVENT_NON_TOPIC_TERMS
            or not 2 <= len(topic) <= 16
            or not re.fullmatch(r"[0-9A-Za-z\u3400-\u9fff_-]+", topic)
        ):
            continue
        topics.append(topic)

    return _bounded_text(_unique_text(topics), 3)


def _swap_first_second_person(value: str) -> str:
    return value.replace("我", "\0").replace("你", "我").replace("\0", "你")


def _directional_relation_query(value: str) -> Optional[DirectionalRelationIntent]:
    """Recognize a small, source-neutral set of asymmetric relationship acts.

    The rules intentionally model roles rather than private names or expected
    places.  Invitation fulfilment is checked before guided travel so a
    comparison such as ``X是不是我第一次赴你的约`` keeps the semantically
    stronger invitation direction instead of becoming a generic trip query.
    """

    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))
    if re.search(r"我.{0,16}(?:来赴|赴)(?:了)?(?:你的)?约", compact):
        return DirectionalRelationIntent("invitation_fulfillment", "invitee")
    if re.search(r"你.{0,16}(?:来赴|赴)(?:了)?(?:我的)?约", compact):
        return DirectionalRelationIntent("invitation_fulfillment", "inviter")
    if re.search(r"你.{0,12}(?:带|领)我.{0,16}(?:去|到)", compact):
        return DirectionalRelationIntent("guided_visit", "guest")
    if re.search(r"我.{0,12}(?:带|领)你.{0,16}(?:去|到)", compact):
        return DirectionalRelationIntent("guided_visit", "guide")
    return None


def _directional_relation_marker(
    content: str,
    role: str,
    intent: DirectionalRelationIntent,
) -> Optional[str]:
    """Return a marker only when source role and relationship direction agree."""

    if role not in {"user", "assistant"}:
        return None
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", content))

    if intent.relation == "invitation_fulfillment":
        speaker_is_invitee = intent.query_speaker_role == "invitee"
        source_speaker_is_invitee = (
            speaker_is_invitee if role == "user" else not speaker_is_invitee
        )
        invitee = "我" if source_speaker_is_invitee else "你"
        inviter = "你" if source_speaker_is_invitee else "我"
        patterns = (
            rf"{invitee}.{{0,18}}(?:来赴|赴)(?:了)?.{{0,4}}{inviter}的约",
            rf"{invitee}.{{0,18}}(?:来赴|赴)(?:了)?约",
            rf"{invitee}.{{0,18}}(?:接受|答应).{{0,6}}{inviter}的邀请",
        )
        matched = next((item for item in patterns if re.search(item, compact)), None)
        if matched is None:
            return None
        # A question or proposal is context for a possible event, not proof
        # that the invitee actually arrived or accepted it.
        if re.search(
            r"(?:愿不愿意|要不要|想不想|可不可以|是否愿意).{0,24}"
            r"(?:来赴|赴|接受|答应)",
            compact,
        ) or re.search(r"愿意.{0,18}(?:来赴|赴).{0,8}约吗", compact):
            return None
        source_role = "invitee" if source_speaker_is_invitee else "inviter"
        return f"invitation_fulfillment:source_speaker_is_{source_role}"

    if intent.relation == "guided_visit":
        if re.search(
            r"(?:想|想要|打算|计划|准备|以后|下次|要不要).{0,16}(?:带|领)",
            compact,
        ):
            return None
        speaker_is_guide = intent.query_speaker_role == "guide"
        source_speaker_is_guide = (
            speaker_is_guide if role == "user" else not speaker_is_guide
        )
        guide = "我" if source_speaker_is_guide else "你"
        guest = "你" if source_speaker_is_guide else "我"
        if not re.search(
            rf"{guide}.{{0,12}}(?:带|领){guest}.{{0,16}}(?:去|到)", compact
        ):
            return None
        source_role = "guide" if source_speaker_is_guide else "guest"
        return f"guided_visit:source_speaker_is_{source_role}"

    return None


def _relationship_direction_payload(
    intent: Optional[DirectionalRelationIntent],
) -> Optional[Dict[str, str]]:
    if intent is None:
        return None
    return {
        "relation": intent.relation,
        "query_speaker_role": intent.query_speaker_role,
        "perspective_policy": (
            "query 我 follows historical user rows; historical assistant rows "
            "may reverse surface pronouns only when semantic roles still agree"
        ),
        "candidate_separation_policy": (
            "opposite inviter/invitee or guide/guest directions are not equivalent"
        ),
    }


def _directional_relation_surface_anchors(
    intent: Optional[DirectionalRelationIntent],
) -> List[str]:
    """Return generic surface forms for both role-correct message perspectives."""

    if intent is None:
        return []
    if intent.relation == "invitation_fulfillment":
        return ["赴你的约", "赴我的约", "来赴约"]
    if intent.relation == "guided_visit":
        return ["你带我", "我带你", "你领我", "我领你"]
    return []


def _shared_event_query_terms(
    normalized: str,
) -> tuple[List[str], List[str], List[str], List[str]]:
    """Build bounded literal rescue anchors without inventing topic synonyms."""

    segments = _focused_query_segments(normalized)
    if not segments:
        return [], [], [], []
    segment = max(segments, key=len)
    for marker in _EARLIEST_QUERY_MARKERS + ("第一回", "当时"):
        segment = segment.replace(marker, "")
    segment = re.sub(r"\s+", "", segment)
    if not segment:
        return [], [], [], []

    topic = segment
    for marker in _SHARED_EVENT_TOPIC_STRIP_MARKERS:
        topic = topic.replace(marker, "")
    topic = topic.strip()
    if topic in _SHARED_EVENT_UNANSWERABLE_TOPICS:
        return [], [], [], []

    directional_relation = _directional_relation_query(normalized)
    variants = _unique_text(
        value
        for value in (topic, segment, _swap_first_second_person(segment))
    )

    def lexical_terms(values: Sequence[str]) -> tuple[List[str], List[str]]:
        fts_terms: List[str] = []
        like_terms: List[str] = []
        for variant in values:
            for lexical in re.findall(
                r"[0-9A-Za-z]+|[\u3400-\u9fff]+", variant
            ):
                if re.fullmatch(r"[\u3400-\u9fff]+", lexical):
                    if len(lexical) == 2:
                        like_terms.append(lexical)
                    elif len(lexical) >= 3:
                        fts_terms.extend(
                            lexical[index : index + 3]
                            for index in range(len(lexical) - 2)
                        )
                elif len(lexical) >= 3:
                    fts_terms.append(lexical)
        return _unique_text(fts_terms), _unique_text(like_terms)

    topic_fts_terms, topic_like_terms = lexical_terms([topic])
    rescue_fts_terms, rescue_like_terms = lexical_terms(variants)
    directional_fts_terms, directional_like_terms = lexical_terms(
        _directional_relation_surface_anchors(directional_relation)
    )
    rescue_fts_terms = _unique_text(rescue_fts_terms + directional_fts_terms)
    rescue_like_terms = _unique_text(rescue_like_terms + directional_like_terms)
    if directional_relation is not None:
        # Both surface perspectives are discoverable, but role-aware window
        # validation below decides whether the direction is semantically valid.
        relation_action_terms = list(rescue_fts_terms)
        relation_action_like_terms = list(rescue_like_terms)
    else:
        relation_action_terms = [
            term
            for term in rescue_fts_terms
            if term.startswith("一起") or term.startswith("共同")
        ]
        relation_action_like_terms = []
    fts_terms = _bounded_text(
        _unique_text(topic_fts_terms + rescue_fts_terms),
        MAX_RELAXED_TERMS_PER_GROUP,
    )
    like_terms = _bounded_text(
        _unique_text(topic_like_terms + rescue_like_terms), MAX_LIKE_TERMS
    )
    anchor_fts_terms = _bounded_text(
        _unique_text(topic_fts_terms + relation_action_terms),
        MAX_RELAXED_TERMS_PER_GROUP,
    )
    anchor_like_terms = _bounded_text(
        _unique_text(topic_like_terms + relation_action_like_terms), MAX_LIKE_TERMS
    )
    return fts_terms, like_terms, anchor_fts_terms, anchor_like_terms


def _shared_event_exclusion_reason(
    content: str, normalized_query: str
) -> Optional[str]:
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", content))
    query_is_creative_event = any(
        marker in normalized_query
        for marker in _SHARED_EVENT_CREATIVE_TOPIC_MARKERS
    )
    if not query_is_creative_event and any(
        marker in normalized for marker in _SHARED_EVENT_CREATIVE_MARKERS
    ):
        return "creative_or_hypothetical_context"
    if _SHARED_EVENT_NEGATIVE_RE.search(normalized):
        return "negated_event"
    if _SHARED_EVENT_FUTURE_RE.search(normalized):
        return "future_plan"
    if _SHARED_EVENT_HYPOTHETICAL_RE.search(normalized):
        return "hypothetical_event"
    if _SHARED_EVENT_INVITATION_RE.search(normalized):
        return "uncompleted_invitation"
    if _SHARED_EVENT_COMPANION_ONLY_RE.search(normalized) and not any(
        marker in normalized
        for marker in ("我也", "我跟你", "我和你", "我们一起", "咱们一起")
    ):
        return "companionship_without_shared_participation"
    return None


def _shared_event_participation_markers(content: str) -> List[str]:
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", content))
    return [
        marker for marker in _SHARED_EVENT_DYADIC_MARKERS if marker in normalized
    ]


def _shared_event_food_context_is_compatible(
    window_rows: Sequence[sqlite3.Row],
    event_rows: Sequence[sqlite3.Row],
    normalized_query: str,
) -> bool:
    """Fail closed when a generic eating query lacks nearby meal evidence."""

    if not any(
        marker in normalized_query
        for marker in _SHARED_EVENT_GENERIC_FOOD_QUERY_MARKERS
    ):
        return True

    event_positions = {
        row["context_position"]
        for row in event_rows
        if "context_position" in row.keys()
        and row["context_position"] is not None
    }
    nearby_contents: List[str] = []
    for row in window_rows:
        position = (
            row["context_position"]
            if "context_position" in row.keys()
            else None
        )
        if event_positions and (
            position is None
            or min(abs(position - event_position) for event_position in event_positions)
            > 2
        ):
            continue
        nearby_contents.append(
            re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(row["content"] or "")))
        )

    if any(
        _SHARED_EVENT_NON_FOOD_CONSUMPTION_RE.search(content)
        for content in nearby_contents
    ):
        return False
    return any(
        marker in content
        for content in nearby_contents
        for marker in _SHARED_EVENT_FOOD_CONTEXT_MARKERS
    )


def _shared_event_evidence_payload(
    row: sqlite3.Row,
    *,
    branch_id: str,
    hit_position: Optional[int],
    matched_terms: Sequence[str],
) -> Dict[str, Any]:
    position = row["context_position"] if "context_position" in row.keys() else None
    relative_position = (
        position - hit_position
        if position is not None and hit_position is not None
        else None
    )
    return {
        "record_id": row["record_id"],
        "content": row["content"],
        "created_at": row["created_at"],
        "source_kind": row["source_kind"],
        "source_ref": row["source_ref"],
        "conversation_id": row["conversation_id"],
        "message_id": row["message_id"],
        "role": row["role"],
        "verified": bool(row["verified"]),
        "authority": row["authority"],
        "conflict_group_id": row["conflict_group_id"],
        "source_cutoff_at": row["source_cutoff_at"],
        "branch_ids": [branch_id],
        "branch_memberships": [
            {
                "branch_id": branch_id,
                "position": position,
                "relative_position": relative_position,
            }
        ],
        "matched_terms": list(matched_terms),
    }


def _judge_shared_event_window(
    window_rows: Sequence[sqlite3.Row],
    *,
    branch_id: str,
    hit_position: Optional[int],
    fts_terms: Sequence[str],
    like_terms: Sequence[str],
    anchor_fts_terms: Sequence[str],
    anchor_like_terms: Sequence[str],
    normalized_query: str,
    directional_relation: Optional[DirectionalRelationIntent] = None,
) -> Optional[SharedEventMatch]:
    """Return a deterministic event match only when literal evidence passes."""

    all_terms = _unique_text(list(fts_terms) + list(like_terms))
    anchor_terms = _unique_text(
        list(anchor_fts_terms) + list(anchor_like_terms)
    )
    participation: List[Dict[str, Any]] = []
    event_rows: List[sqlite3.Row] = []
    payloads: List[Dict[str, Any]] = []

    for row in window_rows[:MAX_SHARED_EVENT_CONTEXT_RECORDS]:
        normalized_content = unicodedata.normalize("NFKC", str(row["content"] or ""))
        matched_terms = [term for term in all_terms if term in normalized_content]
        matched_anchors = [
            term for term in anchor_terms if term in normalized_content
        ]
        payloads.append(
            _shared_event_evidence_payload(
                row,
                branch_id=branch_id,
                hit_position=hit_position,
                matched_terms=matched_terms,
            )
        )
        if not matched_anchors or _shared_event_exclusion_reason(
            normalized_content, normalized_query
        ):
            continue
        if directional_relation is None:
            markers = _shared_event_participation_markers(normalized_content)
        else:
            direction_marker = _directional_relation_marker(
                normalized_content,
                str(row["role"]),
                directional_relation,
            )
            markers = [direction_marker] if direction_marker else []
        if not markers:
            continue
        event_rows.append(row)
        participation.extend(
            {
                "record_id": row["record_id"],
                "role": row["role"],
                "marker": marker,
            }
            for marker in markers
        )

    if not event_rows or not participation:
        return None

    if not _shared_event_food_context_is_compatible(
        window_rows, event_rows, normalized_query
    ):
        return None

    event_rows.sort(
        key=lambda row: (
            row["created_at"],
            row["context_position"]
            if "context_position" in row.keys()
            and row["context_position"] is not None
            else -1,
            row["id"],
        )
    )
    assistant_present = any(row["role"] == "assistant" for row in window_rows)
    identity_sensitive = any(
        marker in normalized_query for marker in ("你", "我们", "我跟你", "你跟我")
    )
    if assistant_present and identity_sensitive:
        support_status = "partial_support"
        identity_status = "historical_assistant_role_only"
        support_reason = (
            "This is the earliest qualifying candidate found by a bounded "
            "same-branch search, but the historical assistant identity is not "
            "verified and an absolute first is not claimed."
        )
    else:
        support_status = "earliest_supported_candidate"
        identity_status = "not_applicable"
        support_reason = (
            "This is the earliest qualifying candidate found by a bounded "
            "same-branch search; it does not prove an absolute first."
        )

    signature = _unique_text(item["record_id"] for item in participation)
    return SharedEventMatch(
        representative_rowid=event_rows[0]["id"],
        event_start_at=event_rows[0]["created_at"],
        event_end_at=event_rows[-1]["created_at"],
        event_evidence=payloads,
        participation_evidence=participation,
        support_status=support_status,
        support_reason=support_reason,
        assistant_identity_status=identity_status,
        signature=signature,
    )


def _relaxed_expression_for_segments(segments: Sequence[str]) -> Optional[str]:
    """Build a bounded relevance expression from already-focused segments."""

    cjk_terms: List[str] = []
    ascii_terms: List[str] = []
    for segment in segments:
        if re.fullmatch(r"[\u3400-\u9fff]+", segment):
            if len(segment) <= 2:
                continue
            if len(segment) <= 4:
                cjk_terms.append(segment)
            else:
                cjk_terms.extend(
                    segment[index : index + 3]
                    for index in range(len(segment) - 2)
                )
        elif len(segment) >= 3:
            ascii_terms.append(segment)

    cjk_terms = _bounded_text(
        _unique_text(cjk_terms), MAX_RELAXED_TERMS_PER_GROUP
    )
    ascii_terms = _bounded_text(
        _unique_text(ascii_terms), MAX_RELAXED_TERMS_PER_GROUP
    )
    ascii_expression = " AND ".join(_quote_fts(term) for term in ascii_terms)
    cjk_expression = " OR ".join(_quote_fts(term) for term in cjk_terms)
    if ascii_expression and cjk_expression:
        return f"({ascii_expression}) AND ({cjk_expression})"
    return ascii_expression or cjk_expression or None


def _recall_query_plan(query: str) -> RecallQueryPlan:
    """Build precision-first exact, relaxed FTS and short-term LIKE tiers.

    FTS5's trigram tokenizer is excellent for exact substrings of at least three
    characters, but a whole natural-language Chinese question is too strict and
    two-character names cannot enter the trigram index.  The planner therefore
    keeps the old exact phrase first, adds a compact alias for all-ASCII names,
    then relaxes long Chinese runs into overlapping trigrams.  One- and
    two-character segments are reserved for the final parameterized LIKE tier.
    """

    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", query)).strip()
    exact_values = [normalized] if len(normalized) >= 3 else []

    ascii_words = re.findall(r"[0-9A-Za-z]+", normalized)
    if (
        len(ascii_words) >= 2
        and re.fullmatch(r"[0-9A-Za-z]+(?:\s+[0-9A-Za-z]+)+", normalized)
    ):
        exact_values.append("".join(ascii_words))

    focused_segments = _focused_query_segments(normalized)
    explicit_literal_anchors = _explicit_query_anchors(normalized)
    explicit_literal_bonus_terms = _explicit_query_bonus_terms(
        normalized, explicit_literal_anchors
    )
    # The most informative clause normally carries the subject; shorter
    # clauses are commonly vocatives or asides ("老朋友", "那时候你好傻").
    # Retain the full question for later fallback.
    primary_focused_segments = (
        [max(focused_segments, key=len)] if focused_segments else []
    )
    focused_fts_terms: List[str] = []
    focused_like_terms: List[str] = []
    for segment in primary_focused_segments:
        if len(segment) >= 3:
            focused_fts_terms.append(segment)
        else:
            focused_like_terms.append(segment)
    focused_fts_terms = _bounded_text(
        _unique_text(focused_fts_terms), MAX_RELAXED_TERMS_PER_GROUP
    )
    focused_like_terms = _bounded_text(
        _unique_text(focused_like_terms), MAX_LIKE_TERMS
    )
    focused_expression = (
        " AND ".join(_quote_fts(term) for term in focused_fts_terms)
        or None
    )
    focused_relaxed_expression = _relaxed_expression_for_segments(
        primary_focused_segments
    )

    like_terms: List[str] = []
    cjk_terms: List[str] = []
    ascii_terms: List[str] = []
    segments = re.findall(
        r"[0-9A-Za-z]+(?:[-_][0-9A-Za-z]+)*|[\u3400-\u9fff]+",
        normalized,
    )
    for segment in segments:
        if re.fullmatch(r"[\u3400-\u9fff]+", segment):
            if len(segment) <= 2:
                like_terms.append(segment)
            elif len(segment) <= 4:
                cjk_terms.append(segment)
            else:
                cjk_terms.extend(
                    segment[index : index + 3]
                    for index in range(len(segment) - 2)
                )
        elif len(segment) >= 3:
            ascii_terms.append(segment)
        else:
            like_terms.append(segment)

    cjk_terms = _bounded_text(
        _unique_text(cjk_terms), MAX_RELAXED_TERMS_PER_GROUP
    )
    ascii_terms = _bounded_text(
        _unique_text(ascii_terms), MAX_RELAXED_TERMS_PER_GROUP
    )

    relaxed_expression = None
    if ascii_terms or cjk_terms:
        ascii_expression = " AND ".join(_quote_fts(term) for term in ascii_terms)
        cjk_expression = " OR ".join(_quote_fts(term) for term in cjk_terms)
        if ascii_expression and cjk_expression:
            relaxed_expression = f"({ascii_expression}) AND ({cjk_expression})"
        elif ascii_expression:
            relaxed_expression = ascii_expression
        else:
            relaxed_expression = cjk_expression

    prefer_oldest = any(
        marker in normalized for marker in _EARLIEST_QUERY_MARKERS
    )
    prefer_latest = any(marker in normalized for marker in _LATEST_QUERY_MARKERS)
    preference_entity = _preference_query_entity(normalized)
    latest_composite_terms = _latest_composite_topic_terms(normalized)
    directional_relation = _directional_relation_query(normalized)
    shared_event_intent = prefer_oldest and (
        directional_relation is not None
        or any(marker in normalized for marker in _SHARED_EVENT_QUERY_MARKERS)
    )
    if shared_event_intent:
        (
            shared_event_fts_terms,
            shared_event_like_terms,
            shared_event_anchor_fts_terms,
            shared_event_anchor_like_terms,
        ) = _shared_event_query_terms(normalized)
    else:
        (
            shared_event_fts_terms,
            shared_event_like_terms,
            shared_event_anchor_fts_terms,
            shared_event_anchor_like_terms,
        ) = ([], [], [], [])
    intent_required_all_like_terms: List[str] = []
    intent_required_any_like_terms: List[str] = []
    intent_bonus_like_terms: List[str] = []
    intent_prefer_compact = False
    intent_min_any_matches = 1
    intent_roles: List[str] = []
    intent_relevance_first = False
    if any(marker in normalized for marker in _MEANING_QUERY_MARKERS):
        intent_required_all_like_terms = [
            segment for segment in primary_focused_segments if len(segment) >= 2
        ][:2]
        # A short literal mention of the entity is not an explanation.  Require
        # at least one source-neutral meaning marker first, then prefer the
        # compact explanatory record over a long keyword dossier.
        intent_required_any_like_terms = list(_MEANING_CONTEXT_TERMS)
        intent_prefer_compact = True
    elif prefer_oldest and any(
        marker in normalized for marker in _GIFT_QUERY_MARKERS
    ):
        # Preserve relationship direction without naming any private gift.
        # At least two generic gift/action terms must co-occur; among equally
        # relevant rows the earliest evidence wins.  The adaptive layer can
        # then trace a later confirmation back to quoted original wording.
        intent_required_any_like_terms = list(_GIFT_RELATION_TERMS)
        intent_bonus_like_terms = list(_GIFT_EARLIEST_BONUS_TERMS)
        if "你送我的" in normalized or "你给我的" in normalized:
            intent_bonus_like_terms.extend(("我送你的", "送给你", "给你的"))
        elif "我送你的" in normalized or "我给你的" in normalized:
            intent_bonus_like_terms.extend(("你送我的", "送给我", "给我的"))
        intent_min_any_matches = 2
        intent_relevance_first = True
    elif "第一次" in normalized and any(
        marker in normalized for marker in ("说爱", "爱我", "我爱你")
    ):
        # Word order differs naturally between a question ("说爱我") and a
        # quoted answer ("我爱你").  Require the temporal marker and use both
        # phrasings only as ranking evidence; never synthesize a date.
        intent_required_any_like_terms = ["我爱你", "爱你"]
        intent_bonus_like_terms = ["亲口", "第一次"]
        intent_roles = ["assistant"]
    elif explicit_literal_anchors:
        # An exact user-supplied phrase/name is stronger than generic latest,
        # preference or long-clause language.  Requiring every explicit handle
        # prevents one common name or two-character phrase from filling the
        # result set with unrelated rows.
        intent_required_all_like_terms = list(explicit_literal_anchors)
        intent_bonus_like_terms = list(explicit_literal_bonus_terms)
        intent_relevance_first = True
    elif preference_entity is not None:
        # Bind the literal subject to generic preference/answer language.  A
        # matching question record may then carry the historical answer in its
        # bounded same-branch context even when the answer omits the subject.
        intent_required_all_like_terms = [preference_entity]
        intent_required_any_like_terms = list(_PREFERENCE_CONTEXT_TERMS)
        intent_bonus_like_terms = list(_PREFERENCE_BONUS_TERMS)
        intent_prefer_compact = True
    elif prefer_latest and len(latest_composite_terms) >= 2:
        # Every literal subject must occur in the evidence row.  Once relevance
        # is guaranteed, newest-first ordering implements "上次/最近一次"
        # without guessing an answer or joining unrelated conversations.
        intent_required_all_like_terms = latest_composite_terms
        intent_bonus_like_terms = list(_LATEST_EVENT_BONUS_TERMS)
    elif "我们" in normalized and "第一次" in normalized:
        # Preserve first-person relationship scope.  Trigram FTS may treat
        # "我们第一次..." and "他们第一次..." as near matches; exact LIKE
        # keeps fictional/third-party events from outranking shared history.
        intent_required_all_like_terms = ["我们"]
        if primary_focused_segments:
            intent_required_any_like_terms = _temporal_topic_terms(
                primary_focused_segments[0]
            )
        intent_bonus_like_terms = ["今天", "刚刚", "第一次"]
        intent_relevance_first = True
    elif prefer_oldest and primary_focused_segments:
        intent_required_any_like_terms = _temporal_topic_terms(
            primary_focused_segments[0]
        )
        if len(intent_required_any_like_terms) > 1:
            intent_min_any_matches = 2

    return RecallQueryPlan(
        normalized_query=normalized,
        exact_expressions=[
            expression
            for value in _unique_text(exact_values)
            if (expression := _fts_expression(value)) is not None
        ],
        focused_expression=focused_expression,
        focused_relaxed_expression=focused_relaxed_expression,
        focused_like_terms=focused_like_terms,
        relaxed_expression=relaxed_expression,
        like_terms=_bounded_text(_unique_text(like_terms), MAX_LIKE_TERMS),
        prefer_oldest=prefer_oldest,
        prefer_latest=prefer_latest,
        intent_required_all_like_terms=intent_required_all_like_terms,
        intent_required_any_like_terms=intent_required_any_like_terms,
        intent_bonus_like_terms=intent_bonus_like_terms,
        intent_prefer_compact=intent_prefer_compact,
        intent_min_any_matches=intent_min_any_matches,
        intent_roles=intent_roles,
        intent_relevance_first=intent_relevance_first,
        explicit_literal_anchors=explicit_literal_anchors,
        shared_event_intent=shared_event_intent,
        shared_event_fts_terms=shared_event_fts_terms,
        shared_event_like_terms=shared_event_like_terms,
        shared_event_anchor_fts_terms=shared_event_anchor_fts_terms,
        shared_event_anchor_like_terms=shared_event_anchor_like_terms,
        directional_relation=directional_relation,
    )


def _escaped_like_pattern(value: str) -> str:
    return (
        "%"
        + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        + "%"
    )


def _literal_anchors_need_priority(plan: RecallQueryPlan) -> bool:
    """Whether a long question should search its explicit handles first."""

    if not plan.explicit_literal_anchors:
        return False

    def compact(value: str) -> str:
        return re.sub(
            r"[^0-9A-Za-z\u3400-\u9fff]+", "", value
        ).casefold()

    query_compact = compact(plan.normalized_query)
    anchors_compact = "".join(
        compact(value) for value in plan.explicit_literal_anchors
    )
    return bool(query_compact and query_compact != anchors_compact)


def _confidence(record: Mapping[str, Any], query: str) -> tuple[float, List[str]]:
    """Return a documented deterministic heuristic, never a probability."""

    score = 0.50
    reasons = ["offline lexical match (+0.50)"]
    if query.casefold() in str(record["content"]).casefold():
        score += 0.20
        reasons.append("exact query substring (+0.20)")
    if bool(record["verified"]):
        score += 0.15
        reasons.append("verified source (+0.15)")
    if str(record["authority"]).casefold() in AUTHORITATIVE_VALUES:
        score += 0.10
        reasons.append("authoritative origin (+0.10)")
    if record["source_ref"]:
        score += 0.05
        reasons.append("traceable source_ref (+0.05)")
    return round(min(score, 1.0), 2), reasons


def _row_to_recall_result(
    row: sqlite3.Row,
    query: str,
    recall_mode: str,
    branch_memberships: Sequence[Mapping[str, Any]],
    conversation_context: Sequence[Mapping[str, Any]] = (),
    event_annotation: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    confidence, confidence_basis = _confidence(row, query)
    result = {
        "record_id": row["record_id"],
        "content": row["content"],
        "created_at": row["created_at"],
        "source_kind": row["source_kind"],
        "source_ref": row["source_ref"],
        "conversation_id": row["conversation_id"],
        "branch_id": row["branch_id"],
        "branch_ids": [item["branch_id"] for item in branch_memberships],
        "branch_memberships": [dict(item) for item in branch_memberships],
        "message_id": row["message_id"],
        "role": row["role"],
        "verified": bool(row["verified"]),
        "authority": row["authority"],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "conflict_group_id": row["conflict_group_id"],
        "source_cutoff_at": row["source_cutoff_at"],
        "recall_mode": recall_mode,
        "conversation_context": [dict(item) for item in conversation_context],
    }
    if event_annotation:
        result.update(dict(event_annotation))
    return result


def recall_records(
    query: str,
    *,
    limit: int = 5,
    as_of: Optional[str] = None,
    created_at_start: Optional[str] = None,
    created_at_end_exclusive: Optional[str] = None,
    db_path: Optional[str] = None,
    agent_id: Optional[str] = None,
    read_only: bool = False,
) -> Dict[str, Any]:
    """Recall V1 records using only local SQLite FTS5 data.

    agent_id 为 None：内部/测试路径，不加可见性谓词（旧行为）。
    agent_id 显式给出：先圈定该 principal 的可见全集（active 校验），
    可见性条件进入 WHERE，先于 bm25/LIKE 排序与 LIMIT；coverage 与正文
    使用同一份可见集合。
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    normalized_as_of = _normalize_timestamp(as_of, "as_of") if as_of else None
    normalized_created_at_start = (
        _normalize_timestamp(created_at_start, "created_at_start")
        if created_at_start
        else None
    )
    normalized_created_at_end = (
        _normalize_timestamp(
            created_at_end_exclusive,
            "created_at_end_exclusive",
        )
        if created_at_end_exclusive
        else None
    )
    if (
        normalized_created_at_start
        and normalized_created_at_end
        and normalized_created_at_start >= normalized_created_at_end
    ):
        raise ValueError(
            "created_at_start must be earlier than created_at_end_exclusive"
        )
    if read_only:
        conn = _connect_readonly(db_path)
    else:
        migrate_records_db(db_path)
        conn = _connect(db_path)
    try:
        visible_rowids_query: Optional[str] = None
        if agent_id is None:
            vis_where = ""
            vis_params: List[Any] = []
        else:
            from .identity import visible_record_rowids_query as visibility_query

            visible_rowids_query, vis_params = visibility_query(conn, agent_id)
            vis_where = (
                f"AND r.id IN ({visible_rowids_query})"
            )
        scope_where = vis_where
        scope_params: List[Any] = list(vis_params)
        if normalized_created_at_start:
            scope_where += "\nAND r.created_at >= ?"
            scope_params.append(normalized_created_at_start)
        if normalized_created_at_end:
            scope_where += "\nAND r.created_at < ?"
            scope_params.append(normalized_created_at_end)
        conn.execute("PRAGMA query_only = ON")
        query_plan = _recall_query_plan(query)

        date_direction = "ASC" if query_plan.prefer_oldest else "DESC"

        def fetch_fts(expression: str) -> List[sqlite3.Row]:
            return conn.execute(
                f"""
                SELECT r.*, bm25(records_v1_fts) AS lexical_rank
                FROM records_v1_fts
                JOIN records_v1 AS r ON r.id = records_v1_fts.rowid
                WHERE records_v1_fts MATCH ?
                {scope_where}
                ORDER BY lexical_rank ASC, r.created_at {date_direction}
                LIMIT ?
                """,
                [expression] + scope_params + [limit],
            ).fetchall()

        def fetch_like(patterns: Sequence[str]) -> List[sqlite3.Row]:
            score_sql = " + ".join(
                "CASE WHEN r.content LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                for _ in patterns
            )
            where_sql = " OR ".join(
                "r.content LIKE ? ESCAPE '\\'" for _ in patterns
            )
            return conn.execute(
                f"""
                SELECT r.*, ({score_sql}) AS lexical_rank
                FROM records_v1 AS r
                WHERE ({where_sql})
                {scope_where}
                ORDER BY lexical_rank DESC, r.created_at {date_direction}
                LIMIT ?
                """,
                list(patterns) + list(patterns) + scope_params + [limit],
            ).fetchall()

        def fetch_ranked_like(
            required_all_terms: Sequence[str],
            required_any_terms: Sequence[str],
            bonus_terms: Sequence[str],
            *,
            prefer_compact: bool = False,
            minimum_any_matches: int = 1,
            roles: Sequence[str] = (),
            relevance_first: bool = False,
        ) -> List[sqlite3.Row]:
            required_all_patterns = [
                _escaped_like_pattern(term) for term in required_all_terms
            ]
            required_any_patterns = [
                _escaped_like_pattern(term) for term in required_any_terms
            ]
            bonus_patterns = [
                _escaped_like_pattern(term) for term in bonus_terms
            ]
            score_patterns = (
                required_all_patterns + required_any_patterns + bonus_patterns
            )
            score_sql = " + ".join(
                "CASE WHEN r.content LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                for _ in score_patterns
            )
            where_parts = [
                "r.content LIKE ? ESCAPE '\\'"
                for _ in required_all_patterns
            ]
            where_params: List[Any] = list(required_all_patterns)
            if required_any_patterns:
                any_score_sql = " + ".join(
                    "CASE WHEN r.content LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                    for _ in required_any_patterns
                )
                where_parts.append(f"({any_score_sql}) >= ?")
                where_params.extend(required_any_patterns)
                where_params.append(minimum_any_matches)
            if roles:
                role_placeholders = ", ".join("?" for _ in roles)
                where_parts.append(f"r.role IN ({role_placeholders})")
                where_params.extend(roles)
            where_sql = " AND ".join(where_parts)
            if query_plan.prefer_oldest and relevance_first:
                order_sql = (
                    "lexical_rank DESC, r.created_at ASC, length(r.content) ASC"
                )
            elif query_plan.prefer_oldest:
                order_sql = (
                    "r.created_at ASC, lexical_rank DESC, length(r.content) ASC"
                )
            elif query_plan.prefer_latest:
                order_sql = (
                    "r.created_at DESC, lexical_rank DESC, length(r.content) ASC"
                )
            elif prefer_compact:
                order_sql = (
                    "length(r.content) ASC, lexical_rank DESC, r.created_at DESC"
                )
            else:
                order_sql = (
                    "lexical_rank DESC, length(r.content) ASC, r.created_at DESC"
                )
            return conn.execute(
                f"""
                SELECT r.*, ({score_sql}) AS lexical_rank
                FROM records_v1 AS r
                WHERE ({where_sql})
                {scope_where}
                ORDER BY {order_sql}
                LIMIT ?
                """,
                score_patterns
                + where_params
                + scope_params
                + [limit],
            ).fetchall()

        def fetch_original_trace(anchors: Sequence[str]) -> List[sqlite3.Row]:
            """Follow literal first-pass anchors once, inside visibility SQL."""

            patterns: List[str] = []
            for anchor in anchors:
                pattern = _escaped_like_pattern(anchor)
                occurrence_count = conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM records_v1 AS r
                    WHERE r.content LIKE ? ESCAPE '\\'
                    {scope_where}
                    """,
                    [pattern] + scope_params,
                ).fetchone()[0]
                # A literal that exists only in the first-pass retelling cannot
                # lead to a distinct source. Removing such singleton prose also
                # prevents recap-only detail from inflating the recap's score.
                if occurrence_count >= 2:
                    patterns.append(pattern)
            if not patterns:
                return []

            minimum_matches = 2 if len(patterns) >= 2 else 1
            score_sql = " + ".join(
                "CASE WHEN r.content LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                for _ in patterns
            )
            return conn.execute(
                f"""
                SELECT r.*, ({score_sql}) AS lexical_rank
                FROM records_v1 AS r
                WHERE ({score_sql}) >= ?
                {scope_where}
                ORDER BY r.created_at ASC, lexical_rank DESC,
                         length(r.content) ASC, r.id ASC
                LIMIT ?
                """,
                patterns
                + patterns
                + [minimum_matches]
                + scope_params
                + [max(limit * 2, limit)],
            ).fetchall()

        def fetch_shared_event_candidates(
            seed_rows: Sequence[sqlite3.Row],
        ) -> tuple[List[sqlite3.Row], bool]:
            """Run one bounded rescue pass ordered by original evidence time."""

            prioritized: Dict[int, sqlite3.Row] = {}
            discovered: Dict[int, sqlite3.Row] = {}
            truncated = False
            fetch_limit = MAX_SHARED_EVENT_CANDIDATES + 1

            anchor_expression = " OR ".join(
                _quote_fts(term)
                for term in query_plan.shared_event_anchor_fts_terms
            )
            dyadic_patterns = [
                _escaped_like_pattern(marker)
                for marker in _SHARED_EVENT_DYADIC_MARKERS
            ]
            if anchor_expression and dyadic_patterns:
                dyadic_where = " OR ".join(
                    "r.content LIKE ? ESCAPE '\\'" for _ in dyadic_patterns
                )
                try:
                    fetched = conn.execute(
                        f"""
                        SELECT r.*, bm25(records_v1_fts) AS lexical_rank
                        FROM records_v1_fts
                        JOIN records_v1 AS r ON r.id = records_v1_fts.rowid
                        WHERE records_v1_fts MATCH ?
                          AND ({dyadic_where})
                        {scope_where}
                        ORDER BY r.created_at ASC, r.id ASC
                        LIMIT ?
                        """,
                        [anchor_expression]
                        + dyadic_patterns
                        + scope_params
                        + [fetch_limit],
                    ).fetchall()
                except sqlite3.OperationalError:
                    fetched = []
                if len(fetched) > MAX_SHARED_EVENT_CANDIDATES:
                    truncated = True
                for row in fetched[:MAX_SHARED_EVENT_CANDIDATES]:
                    prioritized.setdefault(row["id"], row)

            if query_plan.shared_event_anchor_like_terms and dyadic_patterns:
                anchor_patterns = [
                    _escaped_like_pattern(term)
                    for term in query_plan.shared_event_anchor_like_terms
                ]
                dyadic_where = " OR ".join(
                    "r.content LIKE ? ESCAPE '\\'" for _ in dyadic_patterns
                )
                anchor_where = " OR ".join(
                    "r.content LIKE ? ESCAPE '\\'" for _ in anchor_patterns
                )
                fetched = conn.execute(
                    f"""
                    SELECT r.*, 0 AS lexical_rank
                    FROM records_v1 AS r
                    WHERE ({dyadic_where})
                      AND ({anchor_where})
                    {scope_where}
                    ORDER BY r.created_at ASC, r.id ASC
                    LIMIT ?
                    """,
                    dyadic_patterns
                    + anchor_patterns
                    + scope_params
                    + [fetch_limit],
                ).fetchall()
                if len(fetched) > MAX_SHARED_EVENT_CANDIDATES:
                    truncated = True
                for row in fetched[:MAX_SHARED_EVENT_CANDIDATES]:
                    prioritized.setdefault(row["id"], row)

            if query_plan.shared_event_anchor_fts_terms:
                try:
                    fetched = conn.execute(
                        f"""
                        SELECT r.*, bm25(records_v1_fts) AS lexical_rank
                        FROM records_v1_fts
                        JOIN records_v1 AS r ON r.id = records_v1_fts.rowid
                        WHERE records_v1_fts MATCH ?
                        {scope_where}
                        ORDER BY r.created_at ASC, r.id ASC
                        LIMIT ?
                        """,
                        [anchor_expression] + scope_params + [fetch_limit],
                    ).fetchall()
                except sqlite3.OperationalError:
                    fetched = []
                if len(fetched) > MAX_SHARED_EVENT_CANDIDATES:
                    truncated = True
                for row in fetched[:MAX_SHARED_EVENT_CANDIDATES]:
                    discovered.setdefault(row["id"], row)

            if query_plan.shared_event_anchor_like_terms:
                patterns = [
                    _escaped_like_pattern(term)
                    for term in query_plan.shared_event_anchor_like_terms
                ]
                where_sql = " OR ".join(
                    "r.content LIKE ? ESCAPE '\\'" for _ in patterns
                )
                fetched = conn.execute(
                    f"""
                    SELECT r.*, 0 AS lexical_rank
                    FROM records_v1 AS r
                    WHERE ({where_sql})
                    {scope_where}
                    ORDER BY r.created_at ASC, r.id ASC
                    LIMIT ?
                    """,
                    patterns + scope_params + [fetch_limit],
                ).fetchall()
                if len(fetched) > MAX_SHARED_EVENT_CANDIDATES:
                    truncated = True
                for row in fetched[:MAX_SHARED_EVENT_CANDIDATES]:
                    discovered.setdefault(row["id"], row)

            for row in seed_rows:
                discovered.setdefault(row["id"], row)

            prioritized_ordered = sorted(
                prioritized.values(),
                key=lambda row: (row["created_at"], row["id"]),
            )
            remaining = MAX_SHARED_EVENT_CANDIDATES - len(prioritized_ordered)
            filler_ordered = sorted(
                (
                    row
                    for row_id, row in discovered.items()
                    if row_id not in prioritized
                ),
                key=lambda row: (row["created_at"], row["id"]),
            )
            if remaining < 0 or len(filler_ordered) > max(remaining, 0):
                truncated = True
            ordered = (
                prioritized_ordered[:MAX_SHARED_EVENT_CANDIDATES]
                + filler_ordered[: max(remaining, 0)]
            )
            return ordered, truncated

        def evaluate_shared_event_candidates(
            candidates: Sequence[sqlite3.Row],
            *,
            search_truncated: bool,
        ) -> tuple[
            List[sqlite3.Row],
            Dict[int, Dict[str, Any]],
            Dict[str, Any],
        ]:
            relationship_direction = _relationship_direction_payload(
                query_plan.directional_relation
            )
            if not candidates:
                empty_event_recall: Dict[str, Any] = {
                    "intent": "shared_earliest_event",
                    "status": "insufficient_evidence",
                    "candidate_limit": MAX_SHARED_EVENT_CANDIDATES,
                    "candidates_scanned": 0,
                    "windows_evaluated": 0,
                    "qualifying_windows": 0,
                    "search_truncated": search_truncated,
                }
                if relationship_direction is not None:
                    empty_event_recall["relationship_direction"] = (
                        relationship_direction
                    )
                return [], {}, empty_event_recall

            candidate_by_id = {row["id"]: row for row in candidates}
            candidate_rowids = list(candidate_by_id)
            placeholders = ",".join("?" for _ in candidate_rowids)
            memberships_by_rowid: Dict[int, List[sqlite3.Row]] = {}
            for membership in conn.execute(
                "SELECT record_rowid, branch_id, position "
                "FROM records_v1_branch_memberships "
                f"WHERE record_rowid IN ({placeholders}) "
                "AND position IS NOT NULL "
                "ORDER BY record_rowid, branch_id, position",
                candidate_rowids,
            ).fetchall():
                memberships_by_rowid.setdefault(
                    membership["record_rowid"], []
                ).append(membership)

            context_hits: List[tuple[int, str, int, str]] = []
            unpositioned: List[sqlite3.Row] = []
            for row in candidates:
                memberships = memberships_by_rowid.get(row["id"], [])
                if not memberships:
                    unpositioned.append(row)
                    continue
                membership = next(
                    (
                        item
                        for item in memberships
                        if item["branch_id"] == row["branch_id"]
                    ),
                    memberships[0],
                )
                context_hits.append(
                    (
                        row["id"],
                        membership["branch_id"],
                        membership["position"],
                        row["conversation_id"],
                    )
                )

            windows: Dict[int, List[sqlite3.Row]] = {
                row["id"]: [row] for row in unpositioned
            }
            hit_meta: Dict[int, tuple[str, Optional[int]]] = {
                row["id"]: (row["branch_id"], None) for row in unpositioned
            }
            if context_hits:
                values_sql = ", ".join("(?, ?, ?, ?)" for _ in context_hits)
                context_params: List[Any] = []
                for hit in context_hits:
                    context_params.extend(hit)
                    hit_meta[hit[0]] = (hit[1], hit[2])
                context_rows = conn.execute(
                    f"""
                    WITH event_hits(
                        hit_rowid, branch_id, hit_position, conversation_id
                    ) AS (VALUES {values_sql})
                    SELECT h.hit_rowid, h.hit_position,
                           r.*, bm.branch_id AS context_branch_id,
                           bm.position AS context_position
                    FROM event_hits AS h
                    JOIN records_v1_branch_memberships AS bm
                      ON bm.branch_id = h.branch_id
                     AND bm.position BETWEEN
                         max(0, h.hit_position - {SHARED_EVENT_CONTEXT_BEFORE})
                         AND h.hit_position + {SHARED_EVENT_CONTEXT_AFTER}
                    JOIN records_v1 AS r ON r.id = bm.record_rowid
                    WHERE r.conversation_id = h.conversation_id
                    {vis_where}
                    ORDER BY h.hit_rowid, bm.position, r.id
                    """,
                    context_params + vis_params,
                ).fetchall()
                for context_row in context_rows:
                    windows.setdefault(context_row["hit_rowid"], []).append(
                        context_row
                    )

            matches: List[tuple[SharedEventMatch, sqlite3.Row]] = []
            seen_signatures = set()
            for hit_rowid, window_rows in windows.items():
                branch_id, hit_position = hit_meta[hit_rowid]
                match = _judge_shared_event_window(
                    window_rows,
                    branch_id=branch_id,
                    hit_position=hit_position,
                    fts_terms=query_plan.shared_event_fts_terms,
                    like_terms=query_plan.shared_event_like_terms,
                    anchor_fts_terms=query_plan.shared_event_anchor_fts_terms,
                    anchor_like_terms=query_plan.shared_event_anchor_like_terms,
                    normalized_query=query_plan.normalized_query,
                    directional_relation=query_plan.directional_relation,
                )
                if match is None:
                    continue
                signature = (
                    window_rows[0]["conversation_id"],
                    branch_id,
                    tuple(match.signature),
                )
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                representative = next(
                    row
                    for row in window_rows
                    if row["id"] == match.representative_rowid
                )
                matches.append((match, representative))

            matches.sort(
                key=lambda item: (
                    item[0].event_start_at,
                    -len(item[0].participation_evidence),
                    item[0].representative_rowid,
                )
            )
            selected = matches[: min(limit, MAX_SHARED_EVENT_RESULTS)]
            selected_rows = [item[1] for item in selected]
            annotations = {}
            for item in selected:
                annotation: Dict[str, Any] = {
                    "event_evidence": [
                        dict(evidence) for evidence in item[0].event_evidence
                    ],
                    "event_start_at": item[0].event_start_at,
                    "event_end_at": item[0].event_end_at,
                    "event_participation_evidence": [
                        dict(evidence)
                        for evidence in item[0].participation_evidence
                    ],
                    "earliest_support_status": item[0].support_status,
                    "earliest_support_reason": item[0].support_reason,
                    "assistant_identity_status": (
                        item[0].assistant_identity_status
                    ),
                }
                if relationship_direction is not None:
                    annotation["relationship_direction"] = relationship_direction
                annotations[item[0].representative_rowid] = annotation
            status = (
                selected[0][0].support_status
                if selected
                else "insufficient_evidence"
            )
            event_recall_result: Dict[str, Any] = {
                "intent": "shared_earliest_event",
                "status": status,
                "candidate_limit": MAX_SHARED_EVENT_CANDIDATES,
                "candidates_scanned": len(candidates),
                "windows_evaluated": len(windows),
                "qualifying_windows": len(matches),
                "search_truncated": search_truncated,
            }
            if relationship_direction is not None:
                event_recall_result["relationship_direction"] = (
                    relationship_direction
                )
            return selected_rows, annotations, event_recall_result

        rows: List[sqlite3.Row] = []
        recall_mode = "sqlite_fts5_trigram"
        prioritize_literal_anchors = _literal_anchors_need_priority(query_plan)
        if prioritize_literal_anchors:
            recall_mode = "sqlite_like_intent_focused"
            rows = fetch_ranked_like(
                query_plan.intent_required_all_like_terms,
                query_plan.intent_required_any_like_terms,
                query_plan.intent_bonus_like_terms,
                prefer_compact=query_plan.intent_prefer_compact,
                minimum_any_matches=query_plan.intent_min_any_matches,
                roles=query_plan.intent_roles,
                relevance_first=query_plan.intent_relevance_first,
            )
        allow_generic_fallback = not (prioritize_literal_anchors and not rows)

        if not rows and allow_generic_fallback:
            for expression in query_plan.exact_expressions:
                try:
                    rows = fetch_fts(expression)
                except sqlite3.OperationalError:
                    # Query syntax/tokenizer edge cases fail safely into the next
                    # parameterized tier, never into string-built SQL.
                    rows = []
                if rows:
                    break

        if not rows and allow_generic_fallback and (
            query_plan.intent_required_all_like_terms
            or query_plan.intent_required_any_like_terms
        ):
            recall_mode = "sqlite_like_intent_focused"
            rows = fetch_ranked_like(
                query_plan.intent_required_all_like_terms,
                query_plan.intent_required_any_like_terms,
                query_plan.intent_bonus_like_terms,
                prefer_compact=query_plan.intent_prefer_compact,
                minimum_any_matches=query_plan.intent_min_any_matches,
                roles=query_plan.intent_roles,
                relevance_first=query_plan.intent_relevance_first,
            )

        if (
            not rows
            and allow_generic_fallback
            and query_plan.focused_expression
        ):
            recall_mode = "sqlite_fts5_trigram_focused"
            try:
                rows = fetch_fts(query_plan.focused_expression)
            except sqlite3.OperationalError:
                rows = []

        if (
            not rows
            and allow_generic_fallback
            and query_plan.focused_relaxed_expression
        ):
            recall_mode = "sqlite_fts5_trigram_focused_relaxed"
            try:
                rows = fetch_fts(query_plan.focused_relaxed_expression)
            except sqlite3.OperationalError:
                rows = []

        if (
            not rows
            and allow_generic_fallback
            and query_plan.focused_like_terms
        ):
            recall_mode = "sqlite_like_terms_focused"
            rows = fetch_like(
                [
                    _escaped_like_pattern(term)
                    for term in query_plan.focused_like_terms
                ]
            )

        if (
            not rows
            and allow_generic_fallback
            and query_plan.relaxed_expression
        ):
            recall_mode = "sqlite_fts5_trigram_relaxed"
            try:
                rows = fetch_fts(query_plan.relaxed_expression)
            except sqlite3.OperationalError:
                rows = []

        if not rows and allow_generic_fallback and query_plan.like_terms:
            recall_mode = "sqlite_like_terms_fallback"
            like_patterns = [
                _escaped_like_pattern(term) for term in query_plan.like_terms
            ]
            rows = fetch_like(like_patterns)

        if not rows and allow_generic_fallback:
            recall_mode = "sqlite_like_fallback"
            rows = fetch_like(
                [_escaped_like_pattern(query_plan.normalized_query)]
            )

        # A latest composite-event question already pins every literal event
        # subject and returns bounded same-branch context.  A global wording
        # trace from a generic phrase such as "当时怎么说的" can otherwise
        # jump to an older, unrelated event that happens to quote the same
        # sentence.  Explicit latest-event identity therefore wins; callers
        # still receive the nearby historical answer as evidence context.
        suppress_original_trace_for_latest_composite = (
            query_plan.prefer_latest
            and len(query_plan.intent_required_all_like_terms) >= 2
        )
        if (
            rows
            and _wants_original_evidence(query_plan.normalized_query)
            and not suppress_original_trace_for_latest_composite
        ):
            anchors = _original_trace_anchors(rows)
            if anchors:
                traced_rows = fetch_original_trace(anchors)
                if traced_rows:
                    merged_rows: List[sqlite3.Row] = []
                    seen_rowids = set()
                    for row in list(traced_rows) + list(rows):
                        if row["id"] in seen_rowids:
                            continue
                        seen_rowids.add(row["id"])
                        merged_rows.append(row)
                        if len(merged_rows) >= limit:
                            break
                    rows = merged_rows
                    recall_mode = "sqlite_original_wording_trace"

        event_annotations_by_rowid: Dict[int, Dict[str, Any]] = {}
        event_recall: Optional[Dict[str, Any]] = None
        if query_plan.shared_event_intent:
            if not (
                query_plan.shared_event_fts_terms
                or query_plan.shared_event_like_terms
            ):
                rows = []
                recall_mode = "sqlite_shared_event_window"
                event_recall = {
                    "intent": "shared_earliest_event",
                    "status": "insufficient_anchor",
                    "candidate_limit": MAX_SHARED_EVENT_CANDIDATES,
                    "candidates_scanned": 0,
                    "windows_evaluated": 0,
                    "qualifying_windows": 0,
                    "search_truncated": False,
                }
                relationship_direction = _relationship_direction_payload(
                    query_plan.directional_relation
                )
                if relationship_direction is not None:
                    event_recall["relationship_direction"] = relationship_direction
            else:
                candidates, search_truncated = fetch_shared_event_candidates(rows)
                (
                    rows,
                    event_annotations_by_rowid,
                    event_recall,
                ) = evaluate_shared_event_candidates(
                    candidates,
                    search_truncated=search_truncated,
                )
                recall_mode = "sqlite_shared_event_window"

        if agent_id is None:
            coverage = _coverage_from_connection(conn)
            visible_record_count = None
        else:
            from .identity import visible_coverage

            coverage = visible_coverage(conn, agent_id)
            visible_record_count = coverage.pop("_visible_record_count")
        verified_cutoff = coverage["verified_knowledge_cutoff_at"]
        if visible_record_count == 0:
            coverage_status = "no_visible_records"
            coverage_gap = True
        elif verified_cutoff is None:
            coverage_status = "verified_cutoff_unknown"
            coverage_gap = True
        elif normalized_as_of is None:
            coverage_status = "not_assessed_without_as_of"
            coverage_gap = None
        elif normalized_as_of > verified_cutoff:
            coverage_status = "outside_verified_cutoff"
            coverage_gap = True
        else:
            coverage_status = "within_verified_cutoff"
            coverage_gap = False
        coverage.update(
            {
                "requested_as_of": normalized_as_of,
                "coverage_status": coverage_status,
                "coverage_gap": coverage_gap,
            }
        )
        memberships_by_rowid: Dict[int, List[Dict[str, Any]]] = {}
        if rows:
            rowids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in rowids)
            for membership in conn.execute(
                "SELECT record_rowid, branch_id, position "
                "FROM records_v1_branch_memberships "
                f"WHERE record_rowid IN ({placeholders}) "
                "ORDER BY branch_id",
                rowids,
            ).fetchall():
                memberships_by_rowid.setdefault(
                    membership["record_rowid"], []
                ).append(
                    {
                        "branch_id": membership["branch_id"],
                        "position": membership["position"],
                    }
                )
        context_by_rowid: Dict[int, List[Dict[str, Any]]] = {}
        context_hits: List[tuple[int, str, int, str]] = []
        for row in rows:
            memberships = memberships_by_rowid.get(row["id"], [])
            positioned = [
                membership
                for membership in memberships
                if membership["position"] is not None
            ]
            if not positioned:
                continue
            # A shared ancestor may belong to many complete paths.  Its stored
            # primary branch is deterministic and sufficient for a bounded
            # local context window; returning every duplicate path made one
            # recall perform the same visibility scan dozens of times.
            membership = next(
                (
                    item
                    for item in positioned
                    if item["branch_id"] == row["branch_id"]
                ),
                positioned[0],
            )
            context_hits.append(
                (
                    row["id"],
                    membership["branch_id"],
                    membership["position"],
                    row["conversation_id"],
                )
            )

        if context_hits:
            context_before = (
                RECALL_LITERAL_CONTEXT_BEFORE
                if query_plan.explicit_literal_anchors
                else RECALL_CONTEXT_BEFORE
            )
            context_after = (
                RECALL_LITERAL_CONTEXT_AFTER
                if query_plan.explicit_literal_anchors
                else RECALL_CONTEXT_AFTER
            )
            max_context_records = (
                MAX_RECALL_LITERAL_CONTEXT_RECORDS
                if query_plan.explicit_literal_anchors
                else MAX_RECALL_CONTEXT_RECORDS
            )
            values_sql = ", ".join("(?, ?, ?, ?)" for _ in context_hits)
            context_params: List[Any] = []
            for hit in context_hits:
                context_params.extend(hit)
            context_rows = conn.execute(
                f"""
                WITH context_hits(
                    hit_rowid, branch_id, hit_position, conversation_id
                ) AS (VALUES {values_sql})
                SELECT h.hit_rowid, h.hit_position,
                       r.*, bm.branch_id AS context_branch_id,
                       bm.position AS context_position
                FROM context_hits AS h
                JOIN records_v1_branch_memberships AS bm
                  ON bm.branch_id = h.branch_id
                 AND bm.position BETWEEN
                     max(0, h.hit_position - {context_before})
                     AND h.hit_position + {context_after}
                JOIN records_v1 AS r ON r.id = bm.record_rowid
                WHERE r.id != h.hit_rowid
                AND r.conversation_id = h.conversation_id
                {vis_where}
                ORDER BY h.hit_rowid, bm.position, r.id
                """,
                context_params + vis_params,
            ).fetchall()
            for context_row in context_rows:
                relative_position = (
                    context_row["context_position"] - context_row["hit_position"]
                )
                items = context_by_rowid.setdefault(
                    context_row["hit_rowid"], []
                )
                if len(items) >= max_context_records:
                    continue
                items.append(
                    {
                        "record_id": context_row["record_id"],
                        "content": context_row["content"],
                        "created_at": context_row["created_at"],
                        "source_kind": context_row["source_kind"],
                        "source_ref": context_row["source_ref"],
                        "conversation_id": context_row["conversation_id"],
                        "message_id": context_row["message_id"],
                        "role": context_row["role"],
                        "verified": bool(context_row["verified"]),
                        "authority": context_row["authority"],
                        "conflict_group_id": context_row["conflict_group_id"],
                        "source_cutoff_at": context_row["source_cutoff_at"],
                        "branch_ids": [context_row["context_branch_id"]],
                        "branch_memberships": [
                            {
                                "branch_id": context_row["context_branch_id"],
                                "position": context_row["context_position"],
                                "relative_position": relative_position,
                            }
                        ],
                    }
                )
        response = {
            "schema_version": RECALL_SCHEMA_VERSION,
            "query": query,
            "recall_mode": recall_mode,
            "confidence_rule": (
                "Deterministic evidence score: lexical match 0.50, exact "
                "substring 0.20, verified 0.15, authoritative origin 0.10, "
                "traceable source_ref 0.05. It is not a probability."
            ),
            "coverage": coverage,
            "record_time_filter": {
                "applied": bool(
                    normalized_created_at_start or normalized_created_at_end
                ),
                "created_at_start": normalized_created_at_start,
                "created_at_end_exclusive": normalized_created_at_end,
                "semantics": (
                    "primary record timestamps only; conversation context is "
                    "kept as labelled local context"
                ),
            },
            "memories": [
                _row_to_recall_result(
                    row,
                    query,
                    recall_mode,
                    memberships_by_rowid.get(
                        row["id"],
                        [{"branch_id": row["branch_id"], "position": None}],
                    ),
                    context_by_rowid.get(row["id"], []),
                    event_annotations_by_rowid.get(row["id"]),
                )
                for row in rows
            ],
        }
        if event_recall is not None:
            response["event_recall"] = event_recall
        return response
    finally:
        conn.close()
