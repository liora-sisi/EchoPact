import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import backend.memory.records_v1 as records_module
import backend.utils.db as db_module
from backend.memory.records_v1 import (
    COMPACT_RECORD_SCHEMA_VERSION,
    RECORD_SCHEMA_VERSION,
    RecordConflictError,
    RecordPackageError,
    check_records_index_consistency,
    import_record_package,
    migrate_records_db,
    recall_records,
    rebuild_records_index,
)
from backend.trigger.routes import V1RecallRequest, recall_v1_endpoint


FIXTURE = Path(__file__).parent / "fixtures" / "echo_pact_records_v1.json"


def _record_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM records_v1").fetchone()[0]


def _fixture_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_json_import_is_idempotent_and_preserves_branches(tmp_path):
    db_path = tmp_path / "records.db"

    first = import_record_package(str(FIXTURE), db_path=str(db_path), batch_size=2)
    second = import_record_package(str(FIXTURE), db_path=str(db_path), batch_size=2)

    assert first == {
        "schema_version": RECORD_SCHEMA_VERSION,
        "added": 4,
        "skipped": 0,
        "failed": 0,
        "knowledge_cutoff_at": "2026-08-01T00:00:00Z",
        "latest_record_at": "2026-08-05T09:30:00Z",
    }
    assert second["added"] == 0
    assert second["skipped"] == 4
    assert second["failed"] == 0

    with sqlite3.connect(db_path) as conn:
        branches = conn.execute(
            "SELECT branch_id FROM records_v1 "
            "WHERE conversation_id=? AND message_id=? ORDER BY branch_id",
            ("synthetic-lighthouse", "message-001"),
        ).fetchall()
    assert branches == [("alternate",), ("main",)]
    assert check_records_index_consistency(str(db_path))["ok"] is True


def test_jsonl_import_leaves_source_file_unchanged(tmp_path):
    db_path = tmp_path / "records.db"
    jsonl_path = tmp_path / "records.jsonl"
    payload = _fixture_payload()
    lines = []
    for record in payload["records"]:
        record = dict(record, schema_version=RECORD_SCHEMA_VERSION)
        lines.append(json.dumps(record, ensure_ascii=False))
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = hashlib.sha256(jsonl_path.read_bytes()).hexdigest()

    summary = import_record_package(str(jsonl_path), db_path=str(db_path))

    after = hashlib.sha256(jsonl_path.read_bytes()).hexdigest()
    assert summary["added"] == 4
    assert before == after


def test_compact_package_stores_content_once_and_preserves_all_branch_memberships(
    tmp_path,
):
    db_path = tmp_path / "compact.db"
    compact_path = tmp_path / "compact.json"
    base = dict(_fixture_payload()["records"][0])
    base.pop("branch_id")
    base["record_id"] = "compact-shared-message"
    base["schema_version"] = COMPACT_RECORD_SCHEMA_VERSION
    base["branch_memberships"] = [
        {"branch_id": "alternate", "position": 0},
        {"branch_id": "main", "position": 0},
    ]
    compact_path.write_text(
        json.dumps(
            {
                "schema_version": COMPACT_RECORD_SCHEMA_VERSION,
                "records": [base],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = import_record_package(str(compact_path), db_path=str(db_path))
    repeated = import_record_package(str(compact_path), db_path=str(db_path))

    assert first["added"] == 1
    assert first["branch_memberships_added"] == 2
    assert repeated["added"] == 0
    assert repeated["skipped"] == 1
    assert repeated["branch_memberships_added"] == 0
    assert repeated["branch_memberships_skipped"] == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM records_v1").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM records_v1_branch_memberships"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM records_v1_fts_docsize"
        ).fetchone()[0] == 1
    recalled = recall_records("ORCHID-731", db_path=str(db_path))["memories"][0]
    assert recalled["branch_ids"] == ["alternate", "main"]
    assert recalled["branch_memberships"] == [
        {"branch_id": "alternate", "position": 0},
        {"branch_id": "main", "position": 0},
    ]


def test_compact_repeat_can_add_a_new_branch_without_copying_content(tmp_path):
    db_path = tmp_path / "growing-branches.db"
    first_path = tmp_path / "first.json"
    expanded_path = tmp_path / "expanded.json"
    record = dict(_fixture_payload()["records"][0])
    record.pop("branch_id")
    record["record_id"] = "compact-growing-message"
    record["schema_version"] = COMPACT_RECORD_SCHEMA_VERSION
    record["branch_memberships"] = [{"branch_id": "main", "position": 1}]
    first_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": [record]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    expanded_record = dict(record)
    expanded_record["branch_memberships"] = [
        {"branch_id": "alternate", "position": 1},
        {"branch_id": "main", "position": 1},
    ]
    expanded_path.write_text(
        json.dumps(
            {
                "schema_version": COMPACT_RECORD_SCHEMA_VERSION,
                "records": [expanded_record],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    import_record_package(str(first_path), db_path=str(db_path))
    result = import_record_package(str(expanded_path), db_path=str(db_path))

    assert result["added"] == 0
    assert result["skipped"] == 1
    assert result["branch_memberships_added"] == 1
    assert result["branch_memberships_skipped"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM records_v1").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM records_v1_branch_memberships"
        ).fetchone()[0] == 2


def test_compact_branch_position_conflict_fails_without_overwrite(tmp_path):
    db_path = tmp_path / "branch-conflict.db"
    first_path = tmp_path / "first.json"
    conflict_path = tmp_path / "conflict.json"
    record = dict(_fixture_payload()["records"][0])
    record.pop("branch_id")
    record["record_id"] = "compact-position-conflict"
    record["schema_version"] = COMPACT_RECORD_SCHEMA_VERSION
    record["branch_memberships"] = [{"branch_id": "main", "position": 1}]
    first_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": [record]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    conflicting = dict(record)
    conflicting["branch_memberships"] = [{"branch_id": "main", "position": 2}]
    conflict_path.write_text(
        json.dumps(
            {
                "schema_version": COMPACT_RECORD_SCHEMA_VERSION,
                "records": [conflicting],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    import_record_package(str(first_path), db_path=str(db_path))
    with pytest.raises(RecordConflictError):
        import_record_package(str(conflict_path), db_path=str(db_path))

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT position FROM records_v1_branch_memberships"
        ).fetchall() == [(1,)]


def test_same_record_id_with_different_content_fails_without_overwrite(tmp_path):
    db_path = tmp_path / "records.db"
    import_record_package(str(FIXTURE), db_path=str(db_path))
    payload = _fixture_payload()
    payload["records"] = [dict(payload["records"][0])]
    payload["records"][0]["content"] = "冲突内容不得覆盖原记录"
    conflict_path = tmp_path / "conflict.json"
    conflict_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RecordConflictError) as raised:
        import_record_package(str(conflict_path), db_path=str(db_path))

    assert raised.value.summary["added"] == 0
    assert raised.value.summary["failed"] == 1
    assert _record_count(db_path) == 4
    with sqlite3.connect(db_path) as conn:
        content = conn.execute(
            "SELECT content FROM records_v1 WHERE record_id=?",
            ("synthetic-lighthouse-main-001",),
        ).fetchone()[0]
    assert content == "合成事实：灯塔计划的离线代号是 ORCHID-731。"


def test_invalid_package_does_not_pollute_existing_data_or_index(tmp_path):
    db_path = tmp_path / "records.db"
    import_record_package(str(FIXTURE), db_path=str(db_path))
    payload = _fixture_payload()
    del payload["records"][0]["source_ref"]
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RecordPackageError) as raised:
        import_record_package(str(invalid_path), db_path=str(db_path))

    assert raised.value.summary["added"] == 0
    assert raised.value.summary["failed"] == 1
    assert _record_count(db_path) == 4
    assert check_records_index_consistency(str(db_path))["ok"] is True


def test_interrupted_import_resumes_without_duplicates_or_gaps(tmp_path):
    db_path = tmp_path / "records.db"

    def interrupt_after_first_batch(summary):
        if summary["added"] >= 2:
            raise RuntimeError("planned interruption")

    with pytest.raises(RuntimeError, match="planned interruption"):
        import_record_package(
            str(FIXTURE),
            db_path=str(db_path),
            batch_size=2,
            progress_callback=interrupt_after_first_batch,
        )

    assert _record_count(db_path) == 2
    resumed = import_record_package(str(FIXTURE), db_path=str(db_path), batch_size=2)
    assert resumed["added"] == 2
    assert resumed["skipped"] == 2
    assert _record_count(db_path) == 4
    assert check_records_index_consistency(str(db_path))["ok"] is True


def test_record_and_fts_insert_roll_back_together_on_index_failure(tmp_path):
    db_path = tmp_path / "records.db"
    migrate_records_db(str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER synthetic_index_failure
            BEFORE INSERT ON records_v1_index_state
            BEGIN
                SELECT RAISE(ABORT, 'synthetic index failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic index failure"):
        import_record_package(str(FIXTURE), db_path=str(db_path), batch_size=1)

    assert _record_count(db_path) == 0
    assert check_records_index_consistency(str(db_path))["ok"] is True


def test_index_inconsistency_is_detected_and_repeatably_repaired(tmp_path):
    db_path = tmp_path / "records.db"
    import_record_package(str(FIXTURE), db_path=str(db_path))
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, content FROM records_v1 ORDER BY id LIMIT 1"
        ).fetchone()
        conn.execute(
            "INSERT INTO records_v1_fts(records_v1_fts, rowid, content) "
            "VALUES ('delete', ?, ?)",
            row,
        )
        conn.execute(
            "DELETE FROM records_v1_index_state WHERE record_rowid=?",
            (row[0],),
        )

    broken = check_records_index_consistency(str(db_path))
    assert broken["ok"] is False
    assert broken["missing_state"] == [row[0]]
    assert broken["missing_fts"] == [row[0]]

    assert rebuild_records_index(str(db_path))["ok"] is True
    assert rebuild_records_index(str(db_path))["ok"] is True


def test_offline_recall_finds_unique_fact_without_embedding_or_network(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "records.db"
    import_record_package(str(FIXTURE), db_path=str(db_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("USE_REAL_EMBEDDING", "false")
    monkeypatch.setenv("ALLOW_REAL_API_CALLS", "false")

    def network_forbidden(*args, **kwargs):
        pytest.fail("V1 offline recall must not attempt a network call")

    monkeypatch.setattr("requests.post", network_forbidden)
    response = recall_records("ORCHID-731", db_path=str(db_path))

    assert response["recall_mode"] == "sqlite_fts5_trigram"
    assert response["memories"]
    assert any("灯塔计划" in item["content"] for item in response["memories"])
    assert all(
        item["recall_mode"] == "sqlite_fts5_trigram"
        for item in response["memories"]
    )


def test_recall_returns_provenance_conflict_and_explainable_confidence(tmp_path):
    db_path = tmp_path / "records.db"
    import_record_package(str(FIXTURE), db_path=str(db_path))

    response = recall_records("NOVA-9", limit=3, db_path=str(db_path))
    result = response["memories"][0]

    required = {
        "content",
        "created_at",
        "source_kind",
        "source_ref",
        "conversation_id",
        "branch_id",
        "message_id",
        "verified",
        "confidence",
        "conflict_group_id",
        "source_cutoff_at",
        "recall_mode",
    }
    assert required <= result.keys()
    assert result["source_kind"] == "recent_patch"
    assert result["verified"] is False
    assert result["conflict_group_id"] == "synthetic-lighthouse-options"
    assert 0.0 <= result["confidence"] <= 1.0
    assert "not a probability" in response["confidence_rule"]


def test_post_cutoff_query_reports_coverage_gap_without_advancing_cutoff(tmp_path):
    db_path = tmp_path / "records.db"
    import_record_package(str(FIXTURE), db_path=str(db_path))

    response = recall_records(
        "NOVA-9",
        as_of="2026-08-06T00:00:00Z",
        db_path=str(db_path),
    )
    coverage = response["coverage"]

    assert coverage["verified_knowledge_cutoff_at"] == "2026-08-01T00:00:00Z"
    assert coverage["latest_imported_record_at"] == "2026-08-05T09:30:00Z"
    assert coverage["contains_post_cutoff_unverified_recent_patch"] is True
    assert coverage["coverage_status"] == "outside_verified_cutoff"
    assert coverage["coverage_gap"] is True


def test_v1_api_route_uses_temporary_database_and_keeps_legacy_route(tmp_path, monkeypatch):
    db_path = tmp_path / "records.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    import_record_package(str(FIXTURE), db_path=str(db_path))

    response = asyncio.run(
        recall_v1_endpoint(
            V1RecallRequest(
                query="ORCHID-731", as_of="2026-08-01T00:00:00Z"
            )
        )
    )

    assert response["schema_version"] == "echo-pact-recall-v1"
    assert response["coverage"]["coverage_gap"] is False
    from backend.trigger.routes import router

    paths = {route.path for route in router.routes}
    assert "/recall" in paths
    assert "/v1/recall" in paths


def test_migration_is_forward_only_repeatable_and_rolls_back_on_failure(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "migration.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE legacy_sentinel(value TEXT)")
        conn.execute("INSERT INTO legacy_sentinel VALUES ('keep-me')")

    first = migrate_records_db(str(db_path))
    second = migrate_records_db(str(db_path))
    assert first["applied"] == [1, 2]
    assert second["applied"] == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT value FROM legacy_sentinel").fetchone()[0] == "keep-me"

    failing_db = tmp_path / "migration-failure.db"
    with sqlite3.connect(failing_db) as conn:
        conn.execute("CREATE TABLE legacy_sentinel(value TEXT)")
        conn.execute("INSERT INTO legacy_sentinel VALUES ('still-here')")
    monkeypatch.setattr(
        records_module,
        "MIGRATION_1_STATEMENTS",
        records_module.MIGRATION_1_STATEMENTS + ("THIS IS NOT VALID SQL",),
    )
    with pytest.raises(sqlite3.OperationalError):
        migrate_records_db(str(failing_db))
    with sqlite3.connect(failing_db) as conn:
        assert conn.execute("SELECT value FROM legacy_sentinel").fetchone()[0] == "still-here"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='records_v1'"
        ).fetchone()[0] == 0
