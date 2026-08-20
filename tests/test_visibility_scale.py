"""Real-scale guardrails for identity-filtered offline recall.

The production failure that motivated this test appeared when a principal
owned more records than SQLite's bound-variable limit.  Keep the fixture small
and lower that limit deliberately so the regression stays fast in every test
run.
"""

import json
import sqlite3

import backend.memory.records_v1 as records_module
from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package, recall_records


OWNER = "agt-scale-owner"
OUTSIDER = "agt-scale-outsider"


def _record(number: int):
    return {
        "record_id": f"scale-record-{number:03d}",
        "source_kind": "conversation_export",
        "source_ref": f"synthetic://visibility-scale/{number:03d}",
        "conversation_id": "synthetic-visibility-scale",
        "branch_id": "main",
        "message_id": f"scale-message-{number:03d}",
        "role": "user",
        "content": f"规模暗号 synthetic payload {number:03d}",
        "created_at": f"2026-08-01T00:00:{number:02d}Z",
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-08-01T00:00:00Z",
        "conflict_group_id": None,
    }


def test_identity_filtered_recall_does_not_expand_one_variable_per_record(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "visibility-scale.db")
    package_path = tmp_path / "visibility-scale.json"
    package_path.write_text(
        json.dumps(
            {
                "schema_version": "echo-pact-records-v1",
                "records": [_record(number) for number in range(24)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    register_agent(OWNER, "Synthetic scale owner", actor="test", db_path=db_path)
    register_agent(
        OUTSIDER, "Synthetic scale outsider", actor="test", db_path=db_path
    )
    imported = import_record_package(
        str(package_path), db_path=db_path, owner_agent_id=OWNER, actor="test"
    )
    assert imported["added"] == 24

    original_connect = records_module._connect

    def limited_connect(path=None):
        conn = original_connect(path)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
        return conn

    monkeypatch.setattr(records_module, "_connect", limited_connect)

    owner_result = recall_records(
        "规模暗号", limit=5, db_path=db_path, agent_id=OWNER
    )
    assert len(owner_result["memories"]) == 5
    assert all(
        memory["source_ref"].startswith("synthetic://visibility-scale/")
        for memory in owner_result["memories"]
    )
    assert owner_result["coverage"]["latest_imported_record_at"] == (
        "2026-08-01T00:00:23Z"
    )

    outsider_result = recall_records(
        "规模暗号", limit=5, db_path=db_path, agent_id=OUTSIDER
    )
    assert outsider_result["memories"] == []
    assert outsider_result["coverage"]["coverage_status"] == "no_visible_records"
    assert outsider_result["coverage"]["coverage_gap"] is True
