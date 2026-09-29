from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.bad_cases.registry import BadCaseRegistry, BadCaseRegistryError


def _raw_case(
    root: Path,
    *,
    suffix: str = "100-deadbeef",
    query: str = "第一次写 notebook 是什么时候",
    note: str = "召回顺序不对，模型脑袋也没有归纳清楚",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"bad-case-update-{suffix}.json"
    packet = {
        "schema_version": "echo-pact-bad-case-v1",
        "captured_at": "2026-09-20T01:02:03+00:00",
        "note": note,
        "dialogue": [
            {"role": "user", "content": query},
            {"role": "assistant", "content": "旧回答"},
        ],
        "model_status": "ok",
        "recall": {
            "coverage": {"coverage_gap": False},
            "recall_debug": {
                "top_k": [
                    {
                        "position": 1,
                        "record_id": "old-record",
                        "source_channel": "chatgpt",
                        "content_preview": "旧证据",
                    }
                ]
            },
        },
    }
    path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    return path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_initialize_preserves_raw_and_suggests_overlapping_issue_families(tmp_path):
    raw_root = tmp_path / "private" / "bad_cases"
    registry_root = tmp_path / "private" / "registry"
    raw_path = _raw_case(raw_root)
    before = _hash(raw_path)
    registry = BadCaseRegistry(raw_root, registry_root)

    result = registry.initialize(now=lambda: "2026-09-29T00:00:00+00:00")
    card = registry.cards()[0]

    assert result == {"raw_cases": 1, "created": 1, "existing": 0}
    assert _hash(raw_path) == before
    assert card["status"] == "open"
    assert "temporal.first_occurrence" in card["suggested_issue_families"]
    assert "retrieval.ranking" in card["suggested_issue_families"]
    assert "answer.synthesis" in card["suggested_issue_families"]
    assert "coverage.archive_gap" not in card["suggested_issue_families"]
    assert "entity.distinctive_term" not in card["suggested_issue_families"]


def test_suggestions_distinguish_mixed_special_terms_and_named_calendar_dates(tmp_path):
    raw_root = tmp_path / "private" / "bad_cases"
    registry_root = tmp_path / "private" / "registry"
    _raw_case(
        raw_root,
        suffix="101-cafebabe",
        query="拖孩lon第一次出现是在什么时候",
        note="特别词没有召回",
    )
    _raw_case(
        raw_root,
        suffix="102-feedface",
        query="七夕那天我们怎么过的",
        note="模型不知道日期",
    )
    registry = BadCaseRegistry(raw_root, registry_root)
    registry.initialize()
    cards = {card["case_id"]: card for card in registry.cards()}

    special = cards["bad-case-update-101-cafebabe"]["suggested_issue_families"]
    holiday = cards["bad-case-update-102-feedface"]["suggested_issue_families"]
    assert "entity.distinctive_term" in special
    assert "temporal.first_occurrence" in special
    assert "temporal.named_calendar_event" in holiday
    assert "temporal.relative_date" not in holiday


def test_suggestions_surface_repeated_event_timeline_from_review_note(tmp_path):
    raw_root = tmp_path / "private" / "bad_cases"
    registry_root = tmp_path / "private" / "registry"
    _raw_case(
        raw_root,
        suffix="103-01234567",
        query="你陪我去培训还记得吗",
        note="明明有具体哪些日子在培训，可能是脑子不够聪明",
    )
    registry = BadCaseRegistry(raw_root, registry_root)
    registry.initialize()
    families = registry.cards()[0]["suggested_issue_families"]

    assert "temporal.event_timeline" in families
    assert "answer.synthesis" in families


def test_fixed_case_remains_in_report_with_history_and_fix_reference(tmp_path):
    raw_root = tmp_path / "private" / "bad_cases"
    registry_root = tmp_path / "private" / "registry"
    raw_path = _raw_case(raw_root)
    registry = BadCaseRegistry(raw_root, registry_root)
    registry.initialize()
    case_id = raw_path.stem

    registry.review(
        case_id,
        status="fixed",
        issue_families=["temporal.first_occurrence"],
        layers=["query_planning", "ranking"],
        fix_ref="commit:abc123",
        summary="first-occurrence plan now sorts oldest-first",
        note="synthetic acceptance passed",
        now=lambda: "2026-09-29T01:00:00+00:00",
    )
    report = registry.report()
    card = registry.load_card(case_id)

    assert raw_path.exists()
    assert report["case_count"] == 1
    assert report["status_counts"] == {"fixed": 1}
    assert report["cases"][0]["case_id"] == case_id
    assert report["cases"][0]["family_source"] == "reviewed"
    assert len(card["history"]) == 2
    assert card["resolution"]["fix_ref"] == "commit:abc123"


def test_replay_records_pass_fail_and_content_hash_without_copying_text(tmp_path):
    raw_root = tmp_path / "private" / "bad_cases"
    registry_root = tmp_path / "private" / "registry"
    raw_path = _raw_case(raw_root)
    registry = BadCaseRegistry(raw_root, registry_root)
    registry.initialize()
    case_id = raw_path.stem
    registry.review(
        case_id,
        must_recall_any=["wanted-record"],
        must_not_recall=["forbidden-record"],
        note="expected target selected after manual review",
    )

    def runner(query: str) -> dict:
        assert query == "第一次写 notebook 是什么时候"
        return {
            "recall_mode": "synthetic",
            "coverage": {"coverage_gap": False},
            "recall_debug": {
                "top_k": [
                    {
                        "position": 1,
                        "record_id": "wanted-record",
                        "source_channel": "chatgpt",
                        "source_kind": "room-ferry-backup-v1",
                        "content_preview": "private synthetic text",
                    }
                ]
            },
        }

    replay = registry.replay(
        case_id,
        runner=runner,
        now=lambda: "2026-09-29T02:00:00+00:00",
    )
    stored = json.loads(Path(replay["path"]).read_text(encoding="utf-8"))

    assert replay["verdict"] == "passed"
    assert stored["current_top_record_ids"] == ["wanted-record"]
    assert stored["current_top_k"][0]["content_sha256"]
    assert "private synthetic text" not in Path(replay["path"]).read_text(
        encoding="utf-8"
    )
    with pytest.raises(BadCaseRegistryError, match="refusing overwrite"):
        registry.replay(
            case_id,
            runner=runner,
            now=lambda: "2026-09-29T02:00:00+00:00",
        )


def test_raw_tampering_is_detected_and_repository_paths_are_rejected(tmp_path):
    raw_root = tmp_path / "private" / "bad_cases"
    registry_root = tmp_path / "private" / "registry"
    raw_path = _raw_case(raw_root)
    registry = BadCaseRegistry(raw_root, registry_root)
    registry.initialize()
    raw_path.write_text("{}", encoding="utf-8")

    with pytest.raises(BadCaseRegistryError, match="unsupported bad-case schema"):
        registry.cards()

    repository = tmp_path / "repo"
    repository.mkdir()
    with pytest.raises(BadCaseRegistryError, match="outside repository"):
        BadCaseRegistry(repository / "bad_cases", tmp_path / "registry", forbidden_root=repository)


def test_fixed_status_requires_a_fix_reference(tmp_path):
    raw_root = tmp_path / "private" / "bad_cases"
    registry = BadCaseRegistry(raw_root, tmp_path / "private" / "registry")
    case_id = _raw_case(raw_root).stem
    registry.initialize()

    with pytest.raises(BadCaseRegistryError, match="fix reference"):
        registry.review(case_id, status="fixed", note="not enough")
