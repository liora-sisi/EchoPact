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
class RecallQueryPlan:
    """Deterministic, dependency-free tiers for SQLite lexical recall."""

    normalized_query: str
    exact_expressions: Sequence[str]
    focused_expression: Optional[str]
    focused_like_terms: Sequence[str]
    relaxed_expression: Optional[str]
    like_terms: Sequence[str]


MAX_RELAXED_TERMS_PER_GROUP = 24
MAX_LIKE_TERMS = 16
_CJK_QUESTION_SHELLS = (
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
    "以后",
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


def _quote_fts(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _fts_expression(query: str) -> Optional[str]:
    """Return the precision-first exact phrase used by the legacy fast path."""

    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", query)).strip()
    return _quote_fts(normalized) if len(normalized) >= 3 else None


def _focused_query_segments(normalized: str) -> List[str]:
    """Remove common question scaffolding without pretending to understand it."""

    if not re.search(r"[\u3400-\u9fff]", normalized):
        return []
    focused = normalized
    for shell in _CJK_QUESTION_SHELLS:
        focused = focused.replace(shell, " ")
    focused = re.sub(r"^(?:我|你|我们|你们|他|她|它)\s*", "", focused)
    focused = focused.replace("的", " ").replace("很", " ")
    return re.findall(
        r"[0-9A-Za-z]+(?:[-_][0-9A-Za-z]+)*|[\u3400-\u9fff]+",
        focused,
    )


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

    focused_fts_terms: List[str] = []
    focused_like_terms: List[str] = []
    for segment in _focused_query_segments(normalized):
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

    return RecallQueryPlan(
        normalized_query=normalized,
        exact_expressions=[
            expression
            for value in _unique_text(exact_values)
            if (expression := _fts_expression(value)) is not None
        ],
        focused_expression=focused_expression,
        focused_like_terms=focused_like_terms,
        relaxed_expression=relaxed_expression,
        like_terms=_bounded_text(_unique_text(like_terms), MAX_LIKE_TERMS),
    )


def _escaped_like_pattern(value: str) -> str:
    return (
        "%"
        + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        + "%"
    )


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
) -> Dict[str, Any]:
    confidence, confidence_basis = _confidence(row, query)
    return {
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
    }


def recall_records(
    query: str,
    *,
    limit: int = 5,
    as_of: Optional[str] = None,
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
        conn.execute("PRAGMA query_only = ON")
        query_plan = _recall_query_plan(query)

        def fetch_fts(expression: str) -> List[sqlite3.Row]:
            return conn.execute(
                f"""
                SELECT r.*, bm25(records_v1_fts) AS lexical_rank
                FROM records_v1_fts
                JOIN records_v1 AS r ON r.id = records_v1_fts.rowid
                WHERE records_v1_fts MATCH ?
                {vis_where}
                ORDER BY lexical_rank ASC, r.created_at DESC
                LIMIT ?
                """,
                [expression] + vis_params + [limit],
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
                {vis_where}
                ORDER BY lexical_rank DESC, r.created_at DESC
                LIMIT ?
                """,
                list(patterns) + list(patterns) + vis_params + [limit],
            ).fetchall()

        rows: List[sqlite3.Row] = []
        recall_mode = "sqlite_fts5_trigram"
        for expression in query_plan.exact_expressions:
            try:
                rows = fetch_fts(expression)
            except sqlite3.OperationalError:
                # Query syntax/tokenizer edge cases fail safely into the next
                # parameterized tier, never into string-built SQL.
                rows = []
            if rows:
                break

        if not rows and query_plan.focused_expression:
            recall_mode = "sqlite_fts5_trigram_focused"
            try:
                rows = fetch_fts(query_plan.focused_expression)
            except sqlite3.OperationalError:
                rows = []

        if not rows and query_plan.focused_like_terms:
            recall_mode = "sqlite_like_terms_focused"
            rows = fetch_like(
                [
                    _escaped_like_pattern(term)
                    for term in query_plan.focused_like_terms
                ]
            )

        if not rows and query_plan.relaxed_expression:
            recall_mode = "sqlite_fts5_trigram_relaxed"
            try:
                rows = fetch_fts(query_plan.relaxed_expression)
            except sqlite3.OperationalError:
                rows = []

        if not rows and query_plan.like_terms:
            recall_mode = "sqlite_like_terms_fallback"
            like_patterns = [
                _escaped_like_pattern(term) for term in query_plan.like_terms
            ]
            rows = fetch_like(like_patterns)

        if not rows:
            recall_mode = "sqlite_like_fallback"
            rows = fetch_like(
                [_escaped_like_pattern(query_plan.normalized_query)]
            )

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
        return {
            "schema_version": RECALL_SCHEMA_VERSION,
            "query": query,
            "recall_mode": recall_mode,
            "confidence_rule": (
                "Deterministic evidence score: lexical match 0.50, exact "
                "substring 0.20, verified 0.15, authoritative origin 0.10, "
                "traceable source_ref 0.05. It is not a probability."
            ),
            "coverage": coverage,
            "memories": [
                _row_to_recall_result(
                    row,
                    query,
                    recall_mode,
                    memberships_by_rowid.get(
                        row["id"],
                        [{"branch_id": row["branch_id"], "position": None}],
                    ),
                )
                for row in rows
            ],
        }
    finally:
        conn.close()
