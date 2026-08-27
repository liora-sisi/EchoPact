"""Versioned, verified snapshots for the always-on read-only MCP gateway.

The writable Echo Pact database remains the authority.  This module creates a
new SQLite backup, validates that backup through the same strict read-only path
used by MCP, and activates it through a small atomic pointer file.  It never
migrates or edits the source database.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any, Dict, Mapping, Optional

from backend.memory.identity import visible_coverage
from backend.memory.records_v1 import MIGRATION_VERSION, _connect_readonly


SNAPSHOT_MANIFEST_SCHEMA = "echo-pact-cloud-snapshot-v1"
ACTIVE_POINTER_SCHEMA = "echo-pact-cloud-active-pointer-v1"
DATABASE_FILENAME = "echo-pact-readonly.sqlite3"
MANIFEST_FILENAME = "manifest.json"


class CloudSnapshotError(RuntimeError):
    """A cloud snapshot or activation pointer failed closed."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudSnapshotError(f"{field} must be a non-empty string")
    return value.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_text(value: Optional[datetime] = None) -> str:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise CloudSnapshotError("snapshot time must be timezone-aware")
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _load_json(path: Path, expected_schema: str) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CloudSnapshotError(f"{path.name} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != expected_schema:
        raise CloudSnapshotError(f"{path.name} has an unsupported schema")
    return payload


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    data = _canonical_json(payload)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise CloudSnapshotError(f"refusing to overwrite {path.name}") from exc


def _atomic_replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def inspect_database(db_path: str | Path, agent_id: str) -> Dict[str, Any]:
    """Validate one immutable candidate without returning conversation text."""

    database = Path(db_path).expanduser().resolve()
    principal = _required_text(agent_id, "agent_id")
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect_readonly(str(database))
        quick_check = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        if quick_check != ["ok"]:
            raise CloudSnapshotError("SQLite quick_check failed")
        migrations = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        coverage = visible_coverage(conn, principal)
        visible_count = coverage.pop("_visible_record_count")
    except CloudSnapshotError:
        raise
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise CloudSnapshotError("snapshot database validation failed") from exc
    finally:
        if conn is not None:
            conn.close()

    return {
        "database_size": database.stat().st_size,
        "database_sha256": _sha256(database),
        "schema_migrations": migrations,
        "migration_version": MIGRATION_VERSION,
        "agent_id": principal,
        "visible_record_count": visible_count,
        "coverage": coverage,
        "sqlite_quick_check": "ok",
    }


def create_snapshot(
    source_db: str | Path,
    release_root: str | Path,
    agent_id: str,
    *,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Create one new release with SQLite's consistent online backup API."""

    source = Path(source_db).expanduser().resolve()
    if not source.is_file():
        raise CloudSnapshotError("source database does not exist")
    principal = _required_text(agent_id, "agent_id")
    root = Path(release_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    build_dir = Path(tempfile.mkdtemp(prefix=".building-", dir=root))
    snapshot_db = build_dir / DATABASE_FILENAME
    try:
        source_conn = _connect_readonly(str(source))
        try:
            destination = sqlite3.connect(snapshot_db)
            try:
                source_conn.backup(destination)
            finally:
                destination.close()
        finally:
            source_conn.close()

        inspection = inspect_database(snapshot_db, principal)
        created = _utc_text(created_at)
        stamp = datetime.fromisoformat(created.replace("Z", "+00:00")).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        snapshot_id = f"snapshot-{stamp}-{inspection['database_sha256'][:12]}"
        release_dir = root / snapshot_id
        if release_dir.exists():
            raise CloudSnapshotError("snapshot release already exists")

        manifest = {
            "schema_version": SNAPSHOT_MANIFEST_SCHEMA,
            "snapshot_id": snapshot_id,
            "created_at": created,
            "database_file": DATABASE_FILENAME,
            **inspection,
        }
        _write_json_exclusive(build_dir / MANIFEST_FILENAME, manifest)
        os.rename(build_dir, release_dir)
        return {"release_dir": str(release_dir), "manifest": manifest}
    except CloudSnapshotError:
        if build_dir.exists():
            shutil.rmtree(build_dir)
        raise
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        if build_dir.exists():
            shutil.rmtree(build_dir)
        raise CloudSnapshotError("snapshot creation failed") from exc


def verify_release(
    release_dir: str | Path, *, expected_agent_id: Optional[str] = None
) -> Dict[str, Any]:
    """Verify manifest, byte identity, schema, integrity and visibility."""

    release = Path(release_dir).expanduser().resolve()
    if not release.is_dir():
        raise CloudSnapshotError("snapshot release directory does not exist")
    manifest = _load_json(release / MANIFEST_FILENAME, SNAPSHOT_MANIFEST_SCHEMA)
    filename = _required_text(manifest.get("database_file"), "database_file")
    if Path(filename).name != filename:
        raise CloudSnapshotError("database_file must be a plain filename")
    database = release / filename
    if not database.is_file():
        raise CloudSnapshotError("snapshot database is missing")
    manifest_agent = _required_text(manifest.get("agent_id"), "agent_id")
    if expected_agent_id is not None and manifest_agent != expected_agent_id:
        raise CloudSnapshotError("snapshot agent does not match expected identity")

    inspection = inspect_database(database, manifest_agent)
    compared_fields = (
        "database_size",
        "database_sha256",
        "schema_migrations",
        "migration_version",
        "agent_id",
        "visible_record_count",
        "coverage",
        "sqlite_quick_check",
    )
    for field in compared_fields:
        if manifest.get(field) != inspection[field]:
            raise CloudSnapshotError(f"snapshot manifest mismatch: {field}")
    return {
        "release_dir": str(release),
        "database_path": str(database),
        "manifest": manifest,
    }


def _pointer_previous_path(pointer: Path) -> Path:
    return pointer.with_name(pointer.name + ".previous")


def _pointer_payload(verified: Mapping[str, Any]) -> Dict[str, Any]:
    manifest = verified["manifest"]
    return {
        "schema_version": ACTIVE_POINTER_SCHEMA,
        "activated_at": _utc_text(),
        "release_dir": verified["release_dir"],
        "snapshot_id": manifest["snapshot_id"],
        "database_sha256": manifest["database_sha256"],
        "agent_id": manifest["agent_id"],
    }


def resolve_active_snapshot(
    pointer_path: str | Path, *, expected_agent_id: Optional[str] = None
) -> Dict[str, Any]:
    pointer = Path(pointer_path).expanduser().resolve()
    payload = _load_json(pointer, ACTIVE_POINTER_SCHEMA)
    agent_id = _required_text(payload.get("agent_id"), "agent_id")
    if expected_agent_id is not None and agent_id != expected_agent_id:
        raise CloudSnapshotError("active pointer agent does not match expected identity")
    release_dir = _required_text(payload.get("release_dir"), "release_dir")
    verified = verify_release(release_dir, expected_agent_id=agent_id)
    manifest = verified["manifest"]
    for field in ("snapshot_id", "database_sha256", "agent_id"):
        if payload.get(field) != manifest[field]:
            raise CloudSnapshotError(f"active pointer mismatch: {field}")
    return {"pointer": payload, **verified}


def activate_release(
    release_dir: str | Path,
    pointer_path: str | Path,
    *,
    expected_agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically switch the active pointer after full verification."""

    verified = verify_release(release_dir, expected_agent_id=expected_agent_id)
    pointer = Path(pointer_path).expanduser().resolve()
    previous = _pointer_previous_path(pointer)
    old_payload: Optional[Dict[str, Any]] = None
    if pointer.exists():
        old_payload = resolve_active_snapshot(
            pointer, expected_agent_id=expected_agent_id
        )["pointer"]
    new_payload = _pointer_payload(verified)
    if old_payload is not None:
        _atomic_replace_json(previous, old_payload)
    _atomic_replace_json(pointer, new_payload)
    resolved = resolve_active_snapshot(pointer, expected_agent_id=expected_agent_id)
    return {
        "active": resolved["pointer"],
        "database_path": resolved["database_path"],
        "previous_available": previous.is_file(),
    }


def rollback_active(
    pointer_path: str | Path, *, expected_agent_id: Optional[str] = None
) -> Dict[str, Any]:
    """Swap the current and previous verified pointers."""

    pointer = Path(pointer_path).expanduser().resolve()
    previous = _pointer_previous_path(pointer)
    current_payload = resolve_active_snapshot(
        pointer, expected_agent_id=expected_agent_id
    )["pointer"]
    prior_payload = _load_json(previous, ACTIVE_POINTER_SCHEMA)
    prior_agent = _required_text(prior_payload.get("agent_id"), "agent_id")
    if expected_agent_id is not None and prior_agent != expected_agent_id:
        raise CloudSnapshotError("previous pointer agent does not match expected identity")
    verify_release(prior_payload["release_dir"], expected_agent_id=prior_agent)

    _atomic_replace_json(pointer, prior_payload)
    _atomic_replace_json(previous, current_payload)
    resolved = resolve_active_snapshot(pointer, expected_agent_id=expected_agent_id)
    return {
        "active": resolved["pointer"],
        "database_path": resolved["database_path"],
        "previous_available": True,
    }
