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
MIGRATION_VERSION = 3

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
        )
        for version, name, statements in migrations:
            exists = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (version,),
            ).fetchone()
            if exists:
                continue
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


def _coverage_from_connection(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            MAX(CASE
                WHEN verified = 1 AND source_kind <> 'recent_patch'
                THEN source_cutoff_at
            END) AS verified_cutoff,
            MAX(created_at) AS latest_record_at
        FROM records_v1
        """
    ).fetchone()
    verified_cutoff = row["verified_cutoff"] if row else None
    latest_record_at = row["latest_record_at"] if row else None
    recent_row = conn.execute(
        """
        SELECT 1 FROM records_v1
        WHERE source_kind = 'recent_patch'
          AND verified = 0
          AND (? IS NULL OR created_at > ?)
        LIMIT 1
        """,
        (verified_cutoff, verified_cutoff),
    ).fetchone()
    return {
        "verified_knowledge_cutoff_at": verified_cutoff,
        "latest_imported_record_at": latest_record_at,
        "contains_post_cutoff_unverified_recent_patch": bool(recent_row),
    }


def import_record_package(
    path: str,
    *,
    db_path: Optional[str] = None,
    batch_size: int = 100,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Import a validated package in retry-safe transactional batches.

    Validation and conflict preflight happen before inserting any package
    record.  A process interruption can therefore leave only complete batches;
    rerunning the same package skips those record_ids and resumes the remainder.
    """

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    loaded = load_record_package(path)
    migrate_records_db(db_path)

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
        return summary
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


def _fts_expression(query: str) -> Optional[str]:
    terms = re.findall(r"[0-9A-Za-z_-]{3,}|[\u3400-\u9fff]{3,}", query)
    if not terms and len(query.strip()) >= 3:
        terms = [query.strip()]
    unique_terms = list(dict.fromkeys(term for term in terms if len(term) >= 3))
    if not unique_terms:
        return None
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in unique_terms)


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
) -> Dict[str, Any]:
    """Recall V1 records using only local SQLite FTS5 data."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    normalized_as_of = _normalize_timestamp(as_of, "as_of") if as_of else None
    migrate_records_db(db_path)

    conn = _connect(db_path)
    try:
        expression = _fts_expression(query)
        rows: List[sqlite3.Row] = []
        recall_mode = "sqlite_fts5_trigram"
        if expression:
            rows = conn.execute(
                """
                SELECT r.*, bm25(records_v1_fts) AS lexical_rank
                FROM records_v1_fts
                JOIN records_v1 AS r ON r.id = records_v1_fts.rowid
                WHERE records_v1_fts MATCH ?
                ORDER BY lexical_rank ASC, r.created_at DESC
                LIMIT ?
                """,
                (expression, limit),
            ).fetchall()
        if not rows:
            recall_mode = "sqlite_like_fallback"
            rows = conn.execute(
                """
                SELECT r.*, 0 AS lexical_rank
                FROM records_v1 AS r
                WHERE r.content LIKE ? ESCAPE '\\'
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (
                    "%"
                    + query.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                    + "%",
                    limit,
                ),
            ).fetchall()

        coverage = _coverage_from_connection(conn)
        verified_cutoff = coverage["verified_knowledge_cutoff_at"]
        if verified_cutoff is None:
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
