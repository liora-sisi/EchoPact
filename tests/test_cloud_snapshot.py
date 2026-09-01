"""M7 cloud snapshot and rollback tests (synthetic data only)."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

from backend.mcp.cloud_snapshot import (
    CloudSnapshotError,
    activate_release,
    create_snapshot,
    resolve_active_snapshot,
    rollback_active,
    verify_release,
)
from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package


AGENT = "agt-cloud-synthetic"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(record_id: str, content: str, created_at: str):
    return {
        "record_id": record_id,
        "source_kind": "synthetic_cloud_test",
        "source_ref": f"synthetic://cloud/{record_id}",
        "conversation_id": "synthetic-cloud-conversation",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": "user",
        "content": content,
        "created_at": created_at,
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-08-01T00:00:00Z",
        "conflict_group_id": None,
    }


def _source_database(tmp_path: Path, name: str, records: int = 1) -> Path:
    db_path = tmp_path / f"{name} # 100%.sqlite3"
    package_path = tmp_path / f"{name}.json"
    package_path.write_text(
        json.dumps(
            {
                "schema_version": "echo-pact-records-v1",
                "records": [
                    _record(
                        f"{name}-{index}",
                        f"SYNTHETIC-CLOUD-{name}-{index}",
                        f"2026-08-{index + 1:02d}T00:00:00Z",
                    )
                    for index in range(records)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(AGENT, "Synthetic cloud agent", actor="test", db_path=str(db_path))
    summary = import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=AGENT,
        actor="test",
    )
    assert summary["added"] == records
    return db_path


def _exchange(process, message):
    process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def test_create_snapshot_is_consistent_readonly_and_metadata_only(tmp_path):
    source = _source_database(tmp_path, "first", records=2)
    source_hash = _sha256(source)
    releases = tmp_path / "releases"

    created = create_snapshot(
        source,
        releases,
        AGENT,
        created_at=datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc),
    )
    verified = verify_release(created["release_dir"], expected_agent_id=AGENT)
    manifest = verified["manifest"]
    rendered = json.dumps(manifest, ensure_ascii=False)

    assert manifest["visible_record_count"] == 2
    assert manifest["sqlite_quick_check"] == "ok"
    assert manifest["schema_migrations"] == [1, 2, 3, 4, 5, 6]
    assert "SYNTHETIC-CLOUD" not in rendered
    assert str(source) not in rendered
    assert _sha256(source) == source_hash


def test_snapshot_copy_captures_a_committed_wal_generation(tmp_path):
    source = _source_database(tmp_path, "wal", records=1)
    content = "SYNTHETIC-WAL-EXTRA"
    with sqlite3.connect(source) as keeper:
        keeper.execute("PRAGMA journal_mode = WAL")
        keeper.execute("PRAGMA wal_autocheckpoint = 0")
        cursor = keeper.execute(
            "INSERT INTO records_v1 "
            "(schema_version, record_id, source_kind, source_ref, "
            " conversation_id, branch_id, message_id, role, content, "
            " created_at, verified, authority, source_cutoff_at, "
            " conflict_group_id, content_sha256, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "echo-pact-records-v1",
                "wal-extra",
                "synthetic_cloud_test",
                "synthetic://cloud/wal-extra",
                "synthetic-cloud-conversation",
                "main",
                "message-wal-extra",
                "user",
                content,
                "2026-08-03T00:00:00Z",
                0,
                "synthetic-unverified",
                "2026-08-01T00:00:00Z",
                None,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "2026-08-28T00:00:00Z",
            ),
        )
        keeper.execute(
            "INSERT INTO record_visibility_events "
            "(record_rowid, event_kind, target_agent, actor, target_event_seq, "
            " idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cursor.lastrowid,
                "set_owner",
                AGENT,
                "test",
                None,
                "synthetic-cloud-wal-owner",
                "2026-08-28T00:00:00Z",
            ),
        )
        keeper.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0
        created = create_snapshot(source, tmp_path / "releases", AGENT)
    assert created["manifest"]["visible_record_count"] == 2


def test_tamper_and_agent_mismatch_fail_closed(tmp_path):
    source = _source_database(tmp_path, "tamper")
    created = create_snapshot(source, tmp_path / "releases", AGENT)
    release = Path(created["release_dir"])

    with pytest.raises(CloudSnapshotError, match="agent"):
        verify_release(release, expected_agent_id="agt-wrong")

    database = release / "echo-pact-readonly.sqlite3"
    with database.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(CloudSnapshotError, match="database_size|database_sha256"):
        verify_release(release, expected_agent_id=AGENT)


def test_activation_and_rollback_keep_two_verified_generations(tmp_path):
    first_source = _source_database(tmp_path, "generation-one", records=1)
    second_source = _source_database(tmp_path, "generation-two", records=2)
    releases = tmp_path / "releases"
    first = create_snapshot(
        first_source,
        releases,
        AGENT,
        created_at=datetime(2026, 8, 28, 4, 1, tzinfo=timezone.utc),
    )
    second = create_snapshot(
        second_source,
        releases,
        AGENT,
        created_at=datetime(2026, 8, 28, 4, 2, tzinfo=timezone.utc),
    )
    pointer = tmp_path / "state" / "active.json"

    activated_first = activate_release(
        first["release_dir"], pointer, expected_agent_id=AGENT
    )
    assert activated_first["previous_available"] is False
    activated_second = activate_release(
        second["release_dir"], pointer, expected_agent_id=AGENT
    )
    assert activated_second["previous_available"] is True
    assert (
        resolve_active_snapshot(pointer, expected_agent_id=AGENT)["manifest"]
        ["visible_record_count"]
        == 2
    )

    rolled_back = rollback_active(pointer, expected_agent_id=AGENT)
    assert rolled_back["previous_available"] is True
    assert (
        resolve_active_snapshot(pointer, expected_agent_id=AGENT)["manifest"]
        ["visible_record_count"]
        == 1
    )
    rollback_active(pointer, expected_agent_id=AGENT)
    assert (
        resolve_active_snapshot(pointer, expected_agent_id=AGENT)["manifest"]
        ["visible_record_count"]
        == 2
    )


def test_failed_activation_does_not_replace_current_pointer(tmp_path):
    source = _source_database(tmp_path, "stable")
    created = create_snapshot(source, tmp_path / "releases", AGENT)
    pointer = tmp_path / "state" / "active.json"
    activate_release(created["release_dir"], pointer, expected_agent_id=AGENT)
    before = pointer.read_bytes()

    bad_release = tmp_path / "bad-release"
    bad_release.mkdir()
    with pytest.raises(CloudSnapshotError):
        activate_release(bad_release, pointer, expected_agent_id=AGENT)
    assert pointer.read_bytes() == before


def test_failed_create_removes_only_its_build_directory(tmp_path):
    old_db = tmp_path / "old.sqlite3"
    with sqlite3.connect(old_db) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
        )
    releases = tmp_path / "releases"
    with pytest.raises(CloudSnapshotError, match="creation|validation"):
        create_snapshot(old_db, releases, AGENT)
    assert list(releases.iterdir()) == []


def test_cloud_launcher_preserves_m6_recall_contract_and_stays_readonly(tmp_path):
    source = _source_database(tmp_path, "launcher")
    source_hash = _sha256(source)
    direct_gateway = ReadonlyGateway(str(source), AGENT)
    positive_arguments = {
        "query": "SYNTHETIC-CLOUD-launcher-0",
        "limit": 1,
        "include_projection": True,
    }
    future_arguments = {
        "query": "2026年9月1日，SYNTHETIC-CLOUD-launcher-0 是否存在？",
        "limit": 1,
        "as_of": "2026-09-01T12:00:00+08:00",
        "include_projection": True,
    }
    expected_positive = direct_gateway.recall(positive_arguments)
    expected_future_gap = direct_gateway.recall(future_arguments)
    created = create_snapshot(source, tmp_path / "releases", AGENT)
    pointer = tmp_path / "state" / "active.json"
    activated = activate_release(
        created["release_dir"], pointer, expected_agent_id=AGENT
    )
    active_db = Path(activated["database_path"])
    active_hash = _sha256(active_db)

    repo = Path(__file__).resolve().parents[1]
    launcher = repo / "scripts" / "echo_pact_cloud_mcp.py"
    env = os.environ.copy()
    env.update(
        {
            "ECHO_PACT_MCP_SNAPSHOT_POINTER": str(pointer),
            "ECHO_PACT_MCP_AGENT_ID": AGENT,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(launcher)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        initialized = _exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        assert initialized["result"]["serverInfo"]["name"] == "echo-pact-readonly"
        coverage = _exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "memory_coverage", "arguments": {}},
            },
        )
        assert coverage["result"]["structuredContent"]["visible_record_count"] == 1

        positive = _exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "recall_context",
                    "arguments": positive_arguments,
                },
            },
        )["result"]["structuredContent"]
        assert positive == expected_positive
        assert positive["memories"][0]["record_id"] == "launcher-0"
        assert positive["memories"][0]["verified"] is False
        assert positive["memories"][0]["authority"] == "synthetic-unverified"
        assert positive["memories"][0]["source_ref"] == (
            "synthetic://cloud/launcher-0"
        )
        assert "adaptive_recall" in positive
        assert "event_timeline" in positive

        future_gap = _exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "recall_context",
                    "arguments": future_arguments,
                },
            },
        )["result"]["structuredContent"]
        assert future_gap == expected_future_gap
        assert future_gap["memories"] == []
        assert future_gap["recall_mode"] == "sqlite_temporal_coverage_guard"
        assert future_gap["coverage"]["coverage_gap"] is True
        assert future_gap["temporal_coverage"]["status"] == (
            "outside_imported_coverage"
        )
        assert future_gap["event_timeline"]["status"] == (
            "suppressed_outside_imported_coverage"
        )
    finally:
        process.stdin.close()
        process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()

    assert process.returncode == 0
    assert _sha256(source) == source_hash
    assert _sha256(active_db) == active_hash


def test_cloud_launcher_failure_is_generic_and_does_not_leak_paths(tmp_path):
    pointer = tmp_path / "private location" / "active.json"
    repo = Path(__file__).resolve().parents[1]
    launcher = repo / "scripts" / "echo_pact_cloud_mcp.py"
    env = os.environ.copy()
    env.update(
        {
            "ECHO_PACT_MCP_SNAPSHOT_POINTER": str(pointer),
            "ECHO_PACT_MCP_AGENT_ID": AGENT,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, str(launcher)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 2
    assert "snapshot validation failed" in result.stderr
    assert str(pointer) not in result.stderr


def test_cloud_deployment_templates_are_secret_free_and_snapshot_bound():
    repo = Path(__file__).resolve().parents[1]
    profile = (repo / "deploy" / "cloud" / "tunnel-profile.yaml.example").read_text(
        encoding="utf-8"
    )
    environment = (repo / "deploy" / "cloud" / "tunnel.env.example").read_text(
        encoding="utf-8"
    )
    service = (
        repo / "deploy" / "cloud" / "echo-pact-mcp.service.example"
    ).read_text(encoding="utf-8")

    assert "echo_pact_cloud_mcp.py" in profile
    assert "ECHO_PACT_MCP_DB_PATH" not in profile
    assert "${CONTROL_PLANE_API_KEY}" in profile
    assert "tunnel_REPLACE_ME" in profile
    assert "CONTROL_PLANE_API_KEY=\n" in environment
    assert "ECHO_PACT_MCP_SNAPSHOT_POINTER=" in environment
    assert "ProtectSystem=strict" in service
    assert "ReadOnlyPaths=/opt/echo-pact /var/lib/echo-pact" in service
    for text in (profile, environment, service):
        assert "sk-" not in text
        assert re.search(r"tunnel_[0-9a-f]{32}", text) is None
