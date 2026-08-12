"""M5-03 recall join tests; all data is synthetic and local."""

import sqlite3
from pathlib import Path

import pytest

import backend.memory.recall_projection as join_module
import backend.utils.db as db_module
from backend.memory.claim_conflicts import record_decision, register_conflict
from backend.memory.projection import build_projection
from backend.memory.recall_projection import recall_with_projection
from backend.memory.records_v1 import import_record_package, recall_records

FIXTURE = Path(__file__).parent / "fixtures" / "echo_pact_records_v1.json"
REC_MAIN_1 = "synthetic-lighthouse-main-001"
REC_MAIN_2 = "synthetic-lighthouse-main-002"
REC_ALT = "synthetic-lighthouse-alt-001"
REC_PATCH = "synthetic-lighthouse-patch-001"
TABLES = (
    "records_v1", "records_v1_index_state", "records_v1_branch_memberships",
    "projection_runs", "projection_claims", "claim_evidence",
    "claim_conflicts", "conflict_members", "conflict_decisions",
)


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "m503.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    import_record_package(str(FIXTURE), db_path=str(db_path))
    return str(db_path)


def _items():
    return [
        {"claim_key": "lighthouse:codename", "claim_kind": "fact",
         "content": "灯塔计划的离线代号是 ORCHID-731",
         "evidence_record_ids": [REC_MAIN_1, REC_MAIN_2]},
        {"claim_key": "lighthouse:demo-tag", "claim_kind": "fact",
         "content": "灯塔计划的演示标签是 NOVA-9",
         "evidence_record_ids": [REC_PATCH]},
    ]


def _build(db_path, agent_id="agent-a"):
    result = build_projection(
        _items(), agent_id=agent_id, rule_id="synthetic-rule-v1",
        db_path=db_path,
    )
    return [claim["claim_id"] for claim in result["claims"]]


def _hit(result, record_id):
    hit = next((m for m in result["memories"] if m["record_id"] == record_id), None)
    assert hit is not None
    return hit


def _counts(db_path):
    with sqlite3.connect(db_path) as conn:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in TABLES}


def _conflict(db_path, claim_ids):
    return register_conflict(
        "lighthouse-topic", [{"claim_id": cid} for cid in claim_ids],
        agent_id="agent-a", db_path=db_path,
    )["conflict_id"]


def test_attaches_active_claim_and_marks_unprojected(setup_db):
    claim_ids = _build(setup_db)
    projected = recall_with_projection(
        "灯塔计划", agent_id="agent-a", db_path=setup_db
    )
    hit = _hit(projected, REC_MAIN_1)
    assert hit["projection_status"] == "projected"
    assert [claim["claim_id"] for claim in hit["claims"]] == [claim_ids[0]]
    assert hit["claims"][0]["freshness"] == "fresh"
    unprojected = _hit(
        recall_with_projection("琥珀色", agent_id="agent-a", db_path=setup_db),
        REC_ALT,
    )
    assert unprojected["projection_status"] == "unprojected"
    assert unprojected["claims"] == []


def test_no_projection_still_recalls_with_explicit_status(setup_db):
    result = recall_with_projection(
        "灯塔计划", agent_id="agent-a", db_path=setup_db
    )
    assert result["memories"]
    assert all(m["projection_status"] == "unprojected" for m in result["memories"])
    assert result["schema_version"] == "echo-pact-recall-projection-v1"
    assert result["evidence_schema_version"] == "echo-pact-recall-v1"
    assert "不自动选边" in result["adjudication_note"]


def test_open_conflict_is_presented_without_selection(setup_db):
    claim_ids = _build(setup_db)
    _conflict(setup_db, claim_ids)
    annotation = _hit(
        recall_with_projection("灯塔计划", agent_id="agent-a", db_path=setup_db),
        REC_MAIN_1,
    )["claims"][0]["conflicts"][0]
    assert annotation["status"] == "open"
    assert annotation["latest_decision"] is None
    assert annotation["latest_audit_event"] is None


def test_confirm_annotation_uses_stable_target_identity(setup_db):
    claim_ids = _build(setup_db)
    conflict_id = _conflict(setup_db, claim_ids)
    record_decision(
        conflict_id, "confirm_claim", agent_id="agent-a",
        target_claim={"claim_id": claim_ids[0]}, db_path=setup_db,
    )
    annotation = _hit(
        recall_with_projection("灯塔计划", agent_id="agent-a", db_path=setup_db),
        REC_MAIN_1,
    )["claims"][0]["conflicts"][0]
    assert annotation["status"] == "resolved"
    assert annotation["latest_decision"]["target_claim"] == {
        "claim_id": claim_ids[0], "claim_version": 1,
    }
    assert "target_claim_rowid" not in annotation["latest_decision"]


def test_unresolved_audit_after_confirmation_does_not_mask_decision(setup_db):
    claim_ids = _build(setup_db)
    conflict_id = _conflict(setup_db, claim_ids)
    record_decision(
        conflict_id, "confirm_claim", agent_id="agent-a",
        target_claim={"claim_id": claim_ids[0]}, db_path=setup_db,
    )
    record_decision(
        conflict_id, "unresolved", agent_id="agent-a",
        rationale="继续观察但不撤销既有裁决", db_path=setup_db,
    )
    annotation = _hit(
        recall_with_projection("灯塔计划", agent_id="agent-a", db_path=setup_db),
        REC_MAIN_1,
    )["claims"][0]["conflicts"][0]
    assert annotation["status"] == "resolved"
    assert annotation["latest_decision"]["decision"] == "confirm_claim"
    assert annotation["latest_decision"]["target_claim"]["claim_id"] == claim_ids[0]
    assert annotation["latest_audit_event"]["decision"] == "unresolved"


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("keep_all", "resolved"), ("invalidate", "stale")],
)
def test_other_decisions_are_presented(setup_db, decision, expected_status):
    claim_ids = _build(setup_db)
    conflict_id = _conflict(setup_db, claim_ids)
    record_decision(conflict_id, decision, agent_id="agent-a", db_path=setup_db)
    annotation = _hit(
        recall_with_projection("灯塔计划", agent_id="agent-a", db_path=setup_db),
        REC_MAIN_1,
    )["claims"][0]["conflicts"][0]
    assert annotation["status"] == expected_status
    assert annotation["latest_decision"]["decision"] == decision


def test_annotations_do_not_hide_reorder_or_reweight(setup_db):
    claim_ids = _build(setup_db)
    baseline = recall_records("灯塔计划", db_path=setup_db)
    conflict_id = _conflict(setup_db, claim_ids)
    record_decision(
        conflict_id, "confirm_claim", agent_id="agent-a",
        target_claim={"claim_id": claim_ids[0]}, db_path=setup_db,
    )
    joined = recall_with_projection(
        "灯塔计划", agent_id="agent-a", db_path=setup_db
    )
    assert [m["record_id"] for m in joined["memories"]] == [
        m["record_id"] for m in baseline["memories"]
    ]
    assert [m["confidence"] for m in joined["memories"]] == [
        m["confidence"] for m in baseline["memories"]
    ]


def test_evidence_link_drift_is_explicitly_stale(setup_db):
    claim_ids = _build(setup_db)
    with sqlite3.connect(setup_db) as conn:
        claim_rowid = conn.execute(
            "SELECT id FROM projection_claims WHERE claim_id = ?", (claim_ids[0],)
        ).fetchone()[0]
        record_rowid = conn.execute(
            "SELECT id FROM records_v1 WHERE record_id = ?", (REC_PATCH,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO claim_evidence "
            "(claim_rowid, record_rowid, link_kind) VALUES (?, ?, 'supports')",
            (claim_rowid, record_rowid),
        )
    claim = _hit(
        recall_with_projection("灯塔计划", agent_id="agent-a", db_path=setup_db),
        REC_MAIN_1,
    )["claims"][0]
    assert claim["freshness"] == "stale"
    assert claim["stale_reason"] == "source_hash_mismatch"


def test_cross_agent_projection_and_conflict_are_invisible(setup_db):
    claim_ids = _build(setup_db, "agent-a")
    conflict_id = _conflict(setup_db, claim_ids)
    record_decision(conflict_id, "keep_all", agent_id="agent-a", db_path=setup_db)
    hit = _hit(
        recall_with_projection("灯塔计划", agent_id="agent-b", db_path=setup_db),
        REC_MAIN_1,
    )
    assert hit["projection_status"] == "unprojected"
    assert hit["claims"] == []


@pytest.mark.parametrize("bad_agent", [None, "", "   "])
def test_agent_id_is_mandatory(setup_db, bad_agent):
    with pytest.raises(ValueError):
        recall_with_projection("灯塔计划", agent_id=bad_agent, db_path=setup_db)


def test_join_connection_is_sqlite_query_only(setup_db, monkeypatch):
    _build(setup_db)
    real_connect = join_module._connect
    seen = {"blocked": False}

    class ReadOnlyProbe:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, parameters=()):
            cursor = self._conn.execute(sql, parameters)
            if sql.strip().upper() == "PRAGMA QUERY_ONLY = ON":
                with pytest.raises(sqlite3.OperationalError):
                    self._conn.execute(
                        "CREATE TABLE should_never_exist(value TEXT)"
                    )
                seen["blocked"] = True
            return cursor

        def close(self):
            self._conn.close()

    monkeypatch.setattr(join_module, "_connect", lambda path: ReadOnlyProbe(real_connect(path)))
    recall_with_projection("灯塔计划", agent_id="agent-a", db_path=setup_db)
    assert seen["blocked"] is True


def test_recall_join_writes_nothing(setup_db):
    claim_ids = _build(setup_db)
    conflict_id = _conflict(setup_db, claim_ids)
    record_decision(conflict_id, "keep_all", agent_id="agent-a", db_path=setup_db)
    before = _counts(setup_db)
    recall_with_projection("灯塔计划", agent_id="agent-a", db_path=setup_db)
    recall_with_projection("ORCHID", agent_id="agent-a", db_path=setup_db)
    assert _counts(setup_db) == before
