from __future__ import annotations

import json

import pytest

from backend.memory.identity import register_agent
from backend.memory.records_v1 import (
    COMPACT_RECORD_SCHEMA_VERSION,
    import_record_package,
    recall_records,
)


OWNER = "agt-content-phase-owner"


def _record(
    record_id: str,
    content: str,
    *,
    source_kind: str,
    source_ref: str,
    position: int,
) -> dict:
    return {
        "record_id": record_id,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "conversation_id": "synthetic-content-phase",
        "branch_memberships": [{"branch_id": "main", "position": position}],
        "message_id": f"message-{record_id}",
        "role": "assistant",
        "content": content,
        "created_at": "2026-09-29T09:00:00Z",
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-09-29T10:00:00Z",
        "conflict_group_id": None,
    }


@pytest.fixture
def phase_db(tmp_path):
    db_path = tmp_path / "content-phase.sqlite3"
    package_path = tmp_path / "records.json"
    records = [
        _record(
            "ordinary-commentary",
            "正文优先测试：中途记录。",
            source_kind="codex-app-server-v1",
            source_ref="codex-app-server-v1://thread/t/turn/a/item/1?phase=commentary",
            position=0,
        ),
        _record(
            "ordinary-neutral",
            "正文优先测试：生活正文。",
            source_kind="room-ferry-backup-v1",
            source_ref="room-ferry-backup-v1://conversation/a/message/2",
            position=1,
        ),
        _record(
            "ordinary-final",
            "正文优先测试：最终正文。",
            source_kind="codex-app-server-v1",
            source_ref="codex-app-server-v1://thread/t/turn/a/item/3?phase=final_answer",
            position=2,
        ),
        _record(
            "process-final",
            "排序排查过程：最终正文。",
            source_kind="codex-app-server-v1",
            source_ref="codex-app-server-v1://thread/t/turn/b/item/1?phase=final_answer",
            position=3,
        ),
        _record(
            "process-neutral",
            "排序排查过程：生活正文。",
            source_kind="room-ferry-backup-v1",
            source_ref="room-ferry-backup-v1://conversation/b/message/2",
            position=4,
        ),
        _record(
            "process-commentary",
            "排序排查过程：中途记录。",
            source_kind="codex-app-server-v1",
            source_ref="codex-app-server-v1://thread/t/turn/b/item/3?phase=commentary",
            position=5,
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Content phase owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    return db_path


def test_ordinary_question_prefers_final_answer_without_hiding_other_sources(phase_db):
    response = recall_records(
        "正文优先测试",
        limit=3,
        db_path=str(phase_db),
        agent_id=OWNER,
        read_only=True,
        debug=True,
    )

    assert [item["record_id"] for item in response["memories"]] == [
        "ordinary-final",
        "ordinary-neutral",
        "ordinary-commentary",
    ]
    assert response["recall_debug_pass"]["query_plan"][
        "content_phase_preference"
    ] == "final_answer"
    assert response["memories"][0]["retrieval_debug"]["content_phase"] == (
        "final_answer"
    )


def test_explicit_process_question_prefers_commentary_without_hiding_final(phase_db):
    response = recall_records(
        "排序排查过程",
        limit=3,
        db_path=str(phase_db),
        agent_id=OWNER,
        read_only=True,
        debug=True,
    )

    assert [item["record_id"] for item in response["memories"]] == [
        "process-commentary",
        "process-neutral",
        "process-final",
    ]
    assert response["recall_debug_pass"]["query_plan"][
        "content_phase_preference"
    ] == "commentary"
    assert response["memories"][0]["retrieval_debug"]["content_phase"] == (
        "commentary"
    )


def test_chatgpt_folded_reasoning_is_retained_but_does_not_hide_final(tmp_path):
    db_path = tmp_path / "chatgpt-phases.sqlite3"
    package_path = tmp_path / "records.json"
    records = [
        _record(
            "chatgpt-thoughts",
            "折叠阶段测试碎碎念：思考过程。",
            source_kind="chatgpt-official-export-v1",
            source_ref="chatgpt-official-export-v1://conversation/a/message/1?phase=thoughts",
            position=0,
        ),
        _record(
            "chatgpt-recap",
            "折叠阶段测试碎碎念：推理摘要。",
            source_kind="chatgpt-official-export-v1",
            source_ref="chatgpt-official-export-v1://conversation/a/message/2?phase=reasoning_recap",
            position=1,
        ),
        _record(
            "chatgpt-neutral",
            "折叠阶段测试碎碎念：老婆的原问题。",
            source_kind="chatgpt-official-export-v1",
            source_ref="chatgpt-official-export-v1://conversation/a/message/3",
            position=2,
        ),
        _record(
            "chatgpt-final",
            "折叠阶段测试碎碎念：最终正文。",
            source_kind="chatgpt-official-export-v1",
            source_ref="chatgpt-official-export-v1://conversation/a/message/4?phase=final_answer",
            position=3,
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Content phase owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )

    ordinary = recall_records(
        "折叠阶段测试",
        limit=4,
        db_path=str(db_path),
        agent_id=OWNER,
        read_only=True,
        debug=True,
    )
    process = recall_records(
        "折叠阶段测试碎碎念",
        limit=4,
        db_path=str(db_path),
        agent_id=OWNER,
        read_only=True,
        debug=True,
    )

    assert ordinary["memories"][0]["record_id"] == "chatgpt-final"
    assert {item["record_id"] for item in ordinary["memories"]} == {
        "chatgpt-thoughts",
        "chatgpt-recap",
        "chatgpt-neutral",
        "chatgpt-final",
    }
    assert process["memories"][0]["retrieval_debug"]["content_phase"] in {
        "thoughts",
        "reasoning_recap",
    }
