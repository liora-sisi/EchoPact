"""Bounded one-call adaptive recall tests using synthetic records only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory.adaptive_recall import (
    MAX_ADAPTIVE_QUERY_PASSES,
    _explicit_subquestions,
    _subquestion_passes,
    _subquestion_subject_hint,
)
from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package


OWNER = "agt-adaptive-owner"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(record_id: str, content: str, created_at: str, role: str = "user"):
    return {
        "record_id": record_id,
        "source_kind": "synthetic_conversation",
        "source_ref": f"synthetic://adaptive/{record_id}",
        "conversation_id": "synthetic-adaptive",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": role,
        "content": content,
        "created_at": created_at,
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-08-01T00:00:00Z",
        "conflict_group_id": None,
    }


def _database(tmp_path: Path) -> Path:
    db_path = tmp_path / "adaptive.sqlite3"
    package_path = tmp_path / "records.json"
    records = [
        _record(
            "gift-original",
            "我把月光蘑菇灯放到你掌心，我们一起选了暖黄色。",
            "2026-01-02T00:00:00Z",
            "assistant",
        ),
        _record(
            "gift-retelling",
            "后来确认第一件礼物就是那盏灯，当时有一句："
            "“我把月光蘑菇灯放到你掌心，我们一起选了暖黄色。”",
            "2026-07-02T00:00:00Z",
        ),
        _record(
            "academy-class",
            "今天队列训练后去课堂上课，下午还有考试。",
            "2026-02-03T00:00:00Z",
        ),
        _record(
            "academy-canteen",
            "食堂吃完饭再集合，集训作息排得很紧。",
            "2026-02-03T00:05:00Z",
        ),
        _record(
            "meaning-explanation",
            "银纽扣的故事含义是：在陌生城市里彼此照亮。",
            "2026-02-10T00:00:00Z",
        ),
        _record(
            "meaning-casual-mention",
            "今天整理抽屉时又看见那枚银纽扣。",
            "2026-02-11T00:00:00Z",
        ),
        _record(
            "unrelated-trip",
            "我们一起在河边散步，这是普通的共同经历。",
            "2026-03-01T00:00:00Z",
        ),
        _record(
            "exact-anchor",
            "唯一暗号是 ADAPTIVE-2042。",
            "2026-04-01T00:00:00Z",
        ),
        _record(
            "paired-name-evidence",
            "后来核对名字线：月岚先出现，Nova 随后确定；两者并非同刻命名。",
            "2026-04-02T00:00:00Z",
        ),
        _record(
            "paired-name-cjk-noise",
            "月岚也出现在另一份无关的设备清单里。",
            "2026-07-30T00:00:00Z",
        ),
        _record(
            "paired-name-ascii-noise",
            "Nova 也出现在另一份无关的产品说明里。",
            "2026-07-31T00:00:00Z",
        ),
        _record(
            "artifact-state",
            "《晨星木盒》已经选定并告诉店家，等实物到手后再拍照；当时尚未收到。",
            "2026-04-03T00:00:00Z",
        ),
        _record(
            "emoji-explanation",
            "后来解释：21号房里的😮‍💨不是普通叹气，而是那段对话里的特定反应。",
            "2026-04-04T00:00:00Z",
        ),
        _record(
            "scene-title-noise",
            "包间里又点了一次《缓缓同行》，这是只有歌名相同的干扰记录。",
            "2026-07-29T00:00:00Z",
        ),
        _record(
            "scene-multi-anchor",
            "海岛悬崖上的玻璃小屋播放《缓缓同行》；石台放着蓝色钥匙和金色钥匙。",
            "2026-04-05T00:00:00Z",
        ),
        _record(
            "compound-trip-plan",
            "第二次海边旅行原计划去白沙湾，在灯塔旁住两晚。",
            "2026-04-07T00:00:00Z",
        ),
        _record(
            "compound-trip-result",
            "第二次海边旅行后来没有成行，因为暴雨导致交通停运。",
            "2026-04-08T00:00:00Z",
        ),
        _record(
            "first-shared-meal",
            "那天我跟你一起吃烧烤，第一样烤的是五花肉，后来又烤了中翅。",
            "2026-05-12T12:00:00Z",
        ),
        _record(
            "first-shared-meal-retelling",
            "后来提起第一次一起吃东西，我还记得那顿烧烤和五花肉。",
            "2026-07-03T12:00:00Z",
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": "echo-pact-records-v1", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Adaptive owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    return db_path


def test_one_gateway_call_traces_first_gift_to_original_evidence(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "你送我的第一件礼物是什么？", "limit": 5}
    )

    ids = [item["record_id"] for item in response["memories"]]
    assert ids[0] == "gift-original"
    assert "gift-retelling" in ids
    assert response["adaptive_recall"]["external_tool_calls_required"] == 1
    assert response["adaptive_recall"]["query_passes_used"] <= MAX_ADAPTIVE_QUERY_PASSES
    assert response["recall_mode"] == "sqlite_adaptive_bounded"


def test_typo_training_question_recovers_general_training_evidence(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "你陪我在检校培训时发生过什么？", "limit": 5}
    )

    ids = {item["record_id"] for item in response["memories"]}
    assert {"academy-class", "academy-canteen"} <= ids
    passes = {
        item["pass"] for item in response["adaptive_recall"]["passes"]
    }
    assert "training_language" in passes


def test_meaning_question_excludes_short_unexplained_mentions(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "银纽扣在我们这里有什么意思？", "limit": 5}
    )

    ids = [item["record_id"] for item in response["memories"]]
    assert ids[0] == "meaning-explanation"
    assert "meaning-casual-mention" not in ids


def test_negative_shared_event_does_not_expand_into_unrelated_history(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "我们第一次一起登上火星是什么时候？", "limit": 5}
    )

    assert response["memories"] == []
    assert response["event_recall"]["status"] == "insufficient_evidence"
    assert response["adaptive_recall"]["query_passes_used"] == 1
    assert response["adaptive_recall"]["budget_exhausted"] is False


def test_generic_shared_meal_gets_one_call_source_neutral_rescue(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "你还记得我们第一次一起吃东西是什么时候吗？"
            "那次吃了什么，后来怎么提起过？",
            "limit": 5,
        }
    )

    ids = {item["record_id"] for item in response["memories"]}
    assert "first-shared-meal" in ids
    passes = {item["pass"] for item in response["adaptive_recall"]["passes"]}
    assert "shared_event_food_trace" in passes
    assert response["adaptive_recall"]["external_tool_calls_required"] == 1
    assert response["adaptive_recall"]["query_passes_used"] <= (
        MAX_ADAPTIVE_QUERY_PASSES
    )


def test_exact_anchor_keeps_single_fast_pass_and_database_unchanged(tmp_path):
    db_path = _database(tmp_path)
    before = _hash(db_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {"query": "ADAPTIVE-2042", "limit": 1, "include_projection": False}
    )

    assert [item["record_id"] for item in response["memories"]] == [
        "exact-anchor"
    ]
    assert response["adaptive_recall"]["query_passes_used"] == 1
    assert response["recall_mode"] == "sqlite_fts5_trigram"
    assert _hash(db_path) == before


def test_multiple_explicit_anchors_require_same_evidence_row(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "“月岚”和“Nova”是同一时间一起取的吗？先后顺序是什么？",
            "limit": 5,
        }
    )

    ids = [item["record_id"] for item in response["memories"]]
    assert ids[0] == "paired-name-evidence"
    assert "paired-name-cjk-noise" not in ids
    assert "paired-name-ascii-noise" not in ids


def test_artifact_title_beats_long_state_question_scaffolding(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "《晨星木盒》已经收到实物了吗？当时进行到了哪一步？",
            "limit": 5,
        }
    )

    assert response["memories"][0]["record_id"] == "artifact-state"


def test_emoji_and_room_label_stay_bound_together(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "为什么21号房里的“😮‍💨”不是普通叹气？",
            "limit": 5,
        }
    )

    assert response["memories"][0]["record_id"] == "emoji-explanation"


def test_explicit_future_date_suppresses_older_lexical_noise(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "2026年8月3日，唯一暗号是 ADAPTIVE-2042 吗？",
            "limit": 5,
        }
    )

    assert response["memories"] == []
    assert response["recall_mode"] == "sqlite_temporal_coverage_guard"
    assert response["temporal_coverage"]["status"] == (
        "outside_imported_coverage"
    )
    assert response["coverage"]["coverage_gap"] is True
    assert response["event_timeline"]["status"] == (
        "suppressed_outside_imported_coverage"
    )
    assert response["event_timeline"]["mentions"] == []


def test_enumerated_scene_anchors_rerank_title_only_noise(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "海岛、悬崖、玻璃小屋、蓝色钥匙、金色钥匙、石台、"
            "《缓缓同行》",
            "limit": 5,
        }
    )

    ids = [item["record_id"] for item in response["memories"]]
    assert ids[0] == "scene-multi-anchor"
    assert "scene-title-noise" in ids


def test_explicit_subquestions_recover_independent_topics_in_one_call(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "银纽扣在我们这里有什么意思？第二次海边旅行原计划去哪里？",
            "limit": 5,
        }
    )

    ids = {item["record_id"] for item in response["memories"]}
    assert {"meaning-explanation", "compound-trip-plan"} <= ids
    passes = {item["pass"] for item in response["adaptive_recall"]["passes"]}
    assert "subquestion_2" in passes
    assert response["adaptive_recall"]["external_tool_calls_required"] == 1
    assert response["adaptive_recall"]["query_passes_used"] <= (
        MAX_ADAPTIVE_QUERY_PASSES
    )


def test_dependent_subquestion_keeps_literal_subject_without_guessing(tmp_path):
    db_path = _database(tmp_path)
    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "第二次海边旅行原计划去哪里？后来实际有没有成行？",
            "limit": 5,
        }
    )

    ids = {item["record_id"] for item in response["memories"]}
    assert {"compound-trip-plan", "compound-trip-result"} <= ids
    passes = {item["pass"] for item in response["adaptive_recall"]["passes"]}
    assert "subquestion_2_subject" in passes


def test_question_mark_inside_quote_is_not_a_subquestion_boundary():
    assert _explicit_subquestions(
        "“这是真的吗？”是什么意思？后来怎么解释？"
    ) == ["“这是真的吗?”是什么意思", "后来怎么解释"]


def test_subject_hint_removes_generic_version_scaffolding():
    assert _subquestion_subject_hint(
        "星轨木盒的几个版本最后怎么选定"
    ) == "星轨木盒"


def test_unanchored_pronominal_followup_does_not_consume_pass_budget():
    assert _subquestion_passes(
        "这个安排是在解释什么？我为什么离开？"
    ) == []
