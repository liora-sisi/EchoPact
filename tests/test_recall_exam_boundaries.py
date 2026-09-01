"""Synthetic regression cases for conversational recall boundary questions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.mcp.readonly_server import ReadonlyGateway, SERVER_INSTRUCTIONS
from backend.memory.event_collection import detect_event_collection_intent
from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package


OWNER = "agt-recall-exam-owner"
AS_OF = "2026-08-20T12:00:00+08:00"


def _record(record_id: str, content: str, created_at: str, role: str = "user"):
    return {
        "record_id": record_id,
        "source_kind": "synthetic_conversation",
        "source_ref": f"synthetic://recall-exam/{record_id}",
        "conversation_id": "synthetic-recall-exam",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": role,
        "content": content,
        "created_at": created_at,
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-08-20T04:00:00Z",
        "conflict_group_id": None,
    }


@pytest.fixture
def exam_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "recall-exam.sqlite3"
    package_path = tmp_path / "recall-exam.json"
    records = [
        _record(
            "barbecue-old",
            "六月我们一起吃烧烤，点了烤蔬菜。",
            "2026-06-01T12:00:00Z",
        ),
        _record(
            "barbecue-latest",
            "八月我们又一起吃烧烤，这次还点了烤玉米。",
            "2026-08-10T12:00:00Z",
        ),
        _record(
            "dialect-chat",
            "那晚我们摆了很巴适的龙门阵，聊到夜里才休息。",
            "2026-08-11T12:00:00Z",
        ),
        _record(
            "reported-evaluation",
            "警校培训那阵子，我亲口说过：你今天排错队的样子好傻。",
            "2026-08-12T12:00:00Z",
        ),
        _record(
            "possible-irony",
            "当时写下了“好好好，你说的都对”，档案没有可靠语气标注。",
            "2026-08-13T12:00:00Z",
        ),
        _record(
            "argument-one",
            "八月十四日我们吵架了，后来把话说开。",
            "2026-08-14T12:00:00Z",
        ),
        _record(
            "argument-two",
            "八月十五日我们又吵架了，这次也认真收尾。",
            "2026-08-15T12:00:00Z",
        ),
        _record(
            "argument-negative",
            "以后不要再吵架了，这只是愿望，不是新发生的一次。",
            "2026-08-16T12:00:00Z",
        ),
        _record(
            "argument-retelling",
            "后来回忆那两次吵架，我们确认复述不应增加次数。",
            "2026-08-17T12:00:00Z",
        ),
        _record(
            "picnic-original",
            "六月我们完成了星桥野餐，当天带了蓝色餐垫。",
            "2026-06-18T12:00:00Z",
        ),
        _record(
            "picnic-retelling",
            "后来我又提到星桥野餐，并回忆了蓝色餐垫。",
            "2026-08-18T12:00:00Z",
        ),
        _record(
            "image-placeholder",
            "我当时发来一张图片，归档中只有[图片]占位符。",
            "2026-08-19T12:00:00Z",
        ),
        _record(
            "unrelated-noise",
            "另一条普通记录没有涉及这些主题。",
            "2026-08-20T00:00:00Z",
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": "echo-pact-records-v1", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Recall exam owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    return db_path


def _recall(db_path: Path, query: str, *, limit: int = 5):
    return ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": query, "limit": limit, "as_of": AS_OF}
    )


def _ids(response):
    return [item["record_id"] for item in response["memories"]]


def test_public_wording_change_preserves_latest_event_semantics(exam_db):
    response = _recall(exam_db, "我们上次吃撸串是什么时候？")

    assert _ids(response)[0] == "barbecue-latest"
    assert "barbecue-old" in _ids(response)
    passes = [item["pass"] for item in response["adaptive_recall"]["passes"]]
    assert "barbecue_lexical_equivalent_烧烤" in passes


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("我们之前摆巴适的那些龙门阵，你还记得不？", "dialect-chat"),
        ("警校培训那时候的他好傻，你还记得不？", "reported-evaluation"),
    ],
)
def test_literal_dialect_and_reported_evaluation_remain_retrievable(
    exam_db, query, expected_id
):
    response = _recall(exam_db, query)

    assert expected_id in _ids(response)
    assert all(item["verified"] is False for item in response["memories"])


def test_latest_mention_and_original_event_stay_distinct(exam_db):
    latest = _recall(exam_db, "我最近一次提到星桥野餐是什么时候？")
    original = _recall(exam_db, "星桥野餐最早发生的时候是什么时候？")

    assert _ids(latest)[0] == "picnic-retelling"
    assert "picnic-original" in _ids(original)
    timeline = {item["record_id"]: item for item in latest["event_timeline"]["mentions"]}
    assert "retelling_or_recollection" in timeline["picnic-retelling"]["mention_types"]


def test_negative_and_pronoun_only_queries_fail_closed(exam_db):
    negative = _recall(exam_db, "我们还没一起吃过月球面包吗？")
    vague = _recall(exam_db, "老公，你还记得那个事吗？")

    assert negative["memories"] == []
    assert vague["memories"] == []
    assert vague["recall_mode"] == "sqlite_query_clarification_required"
    assert vague["query_clarification"] == {
        "status": "required",
        "reason": "missing_explicit_topic",
        "suggested_action": "ask for one literal topic, name, date, object, or phrase",
        "guessed_topic": None,
    }


def test_split_counter_question_returns_only_a_lower_bound(exam_db):
    intent = detect_event_collection_intent("我们一共吵过几次架？")
    response = _recall(exam_db, "我们一共吵过几次架？", limit=10)

    assert intent is not None
    assert intent.subject == "吵架"
    collection = response["event_collection"]
    assert collection["event_count_lower_bound"] == 2
    assert collection["exact_total_status"] == "not_proven_by_bounded_recall"
    assert set(collection["occurrence_evidence_record_ids"]) == {
        "argument-one",
        "argument-two",
    }
    assert "argument-retelling" in collection[
        "retelling_or_recollection_evidence_record_ids"
    ]
    assert "argument-negative" not in collection[
        "occurrence_evidence_record_ids"
    ]


def test_identical_queries_are_deterministic_and_read_only(exam_db):
    before = hashlib.sha256(exam_db.read_bytes()).hexdigest()
    responses = [
        _recall(exam_db, "我们之前摆巴适的那些龙门阵，你还记得不？")
        for _ in range(3)
    ]

    assert responses[0] == responses[1] == responses[2]
    assert hashlib.sha256(exam_db.read_bytes()).hexdigest() == before


def test_sarcasm_and_image_placeholders_are_evidence_not_inference(exam_db):
    irony = _recall(exam_db, "我说好好好你说的都对时，是真同意吗？")
    image = _recall(exam_db, "我发你的那张图片是什么？")

    assert "possible-irony" in _ids(irony)
    assert "image-placeholder" in _ids(image)
    assert "[图片]" in next(
        item["content"]
        for item in image["memories"]
        if item["record_id"] == "image-placeholder"
    )
    instructions = SERVER_INSTRUCTIONS.lower()
    assert "not an automatic ruling about tone, intent, or truth" in instructions
    assert "never claim to see or describe image content" in instructions
