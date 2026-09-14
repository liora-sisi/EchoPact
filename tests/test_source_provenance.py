"""M12 cross-source provenance tests using synthetic data only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package
from backend.memory.source_provenance import build_source_provenance


OWNER = "agt-source-provenance-owner"
MIRROR_CONTENT = (
    "M12-MIRROR-9917：今晚把蓝色航标灯放在窗边，明早一起核对潮汐刻度。"
)


def _memory(
    record_id: str,
    *,
    source_kind: str,
    created_at: str,
    content: str = MIRROR_CONTENT,
    role: str = "user",
):
    authority = (
        "room-ferry-unverified-backup"
        if source_kind == "room-ferry-backup-v1"
        else "codex-local-unverified-archive"
    )
    return {
        "record_id": record_id,
        "source_kind": source_kind,
        "source_ref": f"synthetic://m12/{record_id}",
        "conversation_id": f"conversation-{source_kind}",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": role,
        "content": content,
        "created_at": created_at,
        "verified": False,
        "authority": authority,
        "source_cutoff_at": "2026-09-01T00:00:00Z",
        "conflict_group_id": None,
    }


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_cross_source_message_is_one_support_unit_without_event_claim():
    annotation = build_source_provenance(
        [
            _memory(
                "chatgpt-message",
                source_kind="room-ferry-backup-v1",
                created_at="2026-09-01T01:02:03.000Z",
            ),
            _memory(
                "codex-message",
                source_kind="codex-app-server-v1",
                created_at="2026-09-01T01:02:03.500Z",
            ),
        ]
    )

    assert annotation["status"] == "multiple_sources_with_exact_mirror_candidates"
    assert annotation["source_channels"] == ["chatgpt", "codex_local"]
    assert len(annotation["exact_cross_source_mirror_groups"]) == 1
    mirror = annotation["exact_cross_source_mirror_groups"][0]
    assert mirror["record_ids"] == ["chatgpt-message", "codex-message"]
    assert mirror["independent_support_units"] == 1
    assert mirror["message_identity_status"] == "exact_mirror_candidate"
    assert mirror["event_identity_status"] == "not_asserted"
    assert mirror["truth_status"] == "not_asserted"
    assert annotation["unresolved_record_ids"] == []
    assert MIRROR_CONTENT not in json.dumps(annotation, ensure_ascii=False)


def test_generic_or_merely_similar_records_are_never_auto_mirrored():
    memories = [
        _memory(
            "chatgpt-short",
            source_kind="room-ferry-backup-v1",
            created_at="2026-09-01T01:00:00Z",
            content="嗯，记得。",
        ),
        _memory(
            "codex-short",
            source_kind="codex-app-server-v1",
            created_at="2026-09-01T01:00:00Z",
            content="嗯，记得。",
        ),
        _memory(
            "chatgpt-late",
            source_kind="room-ferry-backup-v1",
            created_at="2026-09-01T02:00:00Z",
        ),
        _memory(
            "codex-late",
            source_kind="codex-app-server-v1",
            created_at="2026-09-01T02:00:02Z",
        ),
        _memory(
            "codex-similar",
            source_kind="codex-app-server-v1",
            created_at="2026-09-01T03:00:00Z",
            content=MIRROR_CONTENT + " 后来又复述了一次。",
        ),
        _memory(
            "chatgpt-similar",
            source_kind="room-ferry-backup-v1",
            created_at="2026-09-01T03:00:00Z",
        ),
    ]

    annotation = build_source_provenance(memories)

    assert annotation["status"] == "multiple_sources_without_exact_mirror_candidates"
    assert annotation["exact_cross_source_mirror_groups"] == []
    assert annotation["unresolved_record_ids"] == sorted(
        memory["record_id"] for memory in memories
    )


def test_source_provenance_is_deterministic():
    memories = [
        _memory(
            "chatgpt-message",
            source_kind="room-ferry-backup-v1",
            created_at="2026-09-01T01:02:03Z",
        ),
        _memory(
            "codex-message",
            source_kind="codex-app-server-v1",
            created_at="2026-09-01T01:02:03Z",
        ),
    ]

    assert build_source_provenance(memories) == build_source_provenance(
        list(reversed(memories))
    )


def test_gateway_recalls_both_archives_with_provenance_without_writing(tmp_path):
    db_path = tmp_path / "m12-provenance.sqlite3"
    package_path = tmp_path / "m12-records.json"
    records = [
        _memory(
            "chatgpt-message",
            source_kind="room-ferry-backup-v1",
            created_at="2026-09-01T01:02:03Z",
        ),
        _memory(
            "codex-message",
            source_kind="codex-app-server-v1",
            created_at="2026-09-01T01:02:03Z",
        ),
        _memory(
            "codex-related-not-mirror",
            source_kind="codex-app-server-v1",
            created_at="2026-09-01T02:00:00Z",
            content=(
                "M12-MIRROR-9917：后来讨论过蓝色航标灯，但这只是另一条独立测试记录。"
            ),
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": "echo-pact-records-v1", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "M12 synthetic owner", actor="test", db_path=str(db_path))
    summary = import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    assert summary["added"] == 3
    before = _hash(db_path)

    result = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "M12-MIRROR-9917", "limit": 5, "include_projection": False}
    )

    assert {memory["record_id"] for memory in result["memories"]} == {
        "chatgpt-message",
        "codex-message",
        "codex-related-not-mirror",
    }
    assert {memory["source_kind"] for memory in result["memories"]} == {
        "room-ferry-backup-v1",
        "codex-app-server-v1",
    }
    provenance = result["source_provenance"]
    assert provenance["source_channels"] == ["chatgpt", "codex_local"]
    assert len(provenance["exact_cross_source_mirror_groups"]) == 1
    assert provenance["unresolved_record_ids"] == ["codex-related-not-mirror"]
    assert _hash(db_path) == before
