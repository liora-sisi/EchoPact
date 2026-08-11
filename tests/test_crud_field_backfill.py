"""Regression tests for complete legacy Memory row reconstruction."""

import pytest

import backend.utils.db as db_module
from backend.memory.crud import create_memory, get_memory, list_memories
from backend.memory.models import Memory
from backend.utils.db import get_conn, init_db


EXPECTED_PROVENANCE = {
    "agent_id": "synthetic-agent-orbit-01",
    "source_type": "tool",
    "confidence": 0.42,
    "conflict_group_id": "synthetic-conflict-001",
    "last_verified_at": "2042-03-04T05:06:07+00:00",
    "recall_reason": "Synthetic provenance regression sentinel.",
}


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    db_module.DB_PATH = str(tmp_path / "test.db")
    init_db()


def _create_non_default_row() -> int:
    mem_id = create_memory(
        Memory(
            content="SYNTHETIC-ORBIT-2042 provenance fixture",
            agent_id="synthetic-bootstrap-agent",
            source_type="model",
        )
    )
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE memories
            SET agent_id = ?, source_type = ?, confidence = ?,
                conflict_group_id = ?, last_verified_at = ?, recall_reason = ?
            WHERE id = ?
            """,
            (
                EXPECTED_PROVENANCE["agent_id"],
                EXPECTED_PROVENANCE["source_type"],
                EXPECTED_PROVENANCE["confidence"],
                EXPECTED_PROVENANCE["conflict_group_id"],
                EXPECTED_PROVENANCE["last_verified_at"],
                EXPECTED_PROVENANCE["recall_reason"],
                mem_id,
            ),
        )
    return mem_id


def _assert_provenance(loaded: Memory) -> None:
    for field, expected in EXPECTED_PROVENANCE.items():
        assert getattr(loaded, field) == expected, (
            f"field {field} silently fell back: "
            f"{getattr(loaded, field)!r} != {expected!r}"
        )


def test_get_memory_preserves_non_default_provenance_fields():
    loaded = get_memory(_create_non_default_row(), agent_id=EXPECTED_PROVENANCE["agent_id"])
    assert loaded is not None
    _assert_provenance(loaded)


def test_list_memories_preserves_non_default_provenance_fields():
    _create_non_default_row()
    loaded = list_memories(
        limit=5,
        agent_id=EXPECTED_PROVENANCE["agent_id"],
    )
    assert len(loaded) == 1
    _assert_provenance(loaded[0])
