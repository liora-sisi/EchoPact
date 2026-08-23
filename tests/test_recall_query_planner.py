"""Synthetic regression tests for natural-language SQLite recall planning."""

from __future__ import annotations

import json

import pytest

from backend.memory.identity import register_agent
from backend.memory.records_v1 import (
    COMPACT_RECORD_SCHEMA_VERSION,
    MAX_LIKE_TERMS,
    MAX_RELAXED_TERMS_PER_GROUP,
    _recall_query_plan,
    import_record_package,
    recall_records,
)


OWNER = "agt-query-owner"
OUTSIDER = "agt-query-outsider"


def _record(
    record_id: str,
    content: str,
    created_at: str,
    *,
    role: str = "user",
) -> dict:
    return {
        "record_id": record_id,
        "source_kind": "synthetic_conversation",
        "source_ref": f"synthetic://recall-query/{record_id}",
        "conversation_id": "synthetic-recall-query",
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


@pytest.fixture
def query_db(tmp_path):
    db_path = tmp_path / "recall-query.sqlite3"
    package_path = tmp_path / "records.json"
    records = [
        _record(
            "cats",
            "豆豆和贝贝是家里的两只猫。",
            "2026-07-01T00:00:00Z",
        ),
        _record(
            "night-shift",
            "下完夜班以后先睡觉休息。",
            "2026-07-02T00:00:00Z",
        ),
        _record(
            "echo-pact",
            "Echo Pact 是来源无关的外置记忆基础设施。",
            "2026-07-03T00:00:00Z",
        ),
        _record(
            "deepseek",
            "DeepSeek 写下了早期框架。",
            "2026-07-04T00:00:00Z",
        ),
        _record(
            "echo-only",
            "Echo 是一个单独出现的英文词。",
            "2026-07-05T00:00:00Z",
        ),
        _record(
            "pact-only",
            "Pact 也是一个单独出现的英文词。",
            "2026-07-06T00:00:00Z",
        ),
        _record(
            "deep-only",
            "Deep 只用于制造宽泛匹配干扰。",
            "2026-07-07T00:00:00Z",
        ),
        _record(
            "seek-only",
            "Seek 只用于制造宽泛匹配干扰。",
            "2026-07-08T00:00:00Z",
        ),
        _record(
            "name-only",
            "给测试文件取名字时保持确定性。",
            "2026-07-09T00:00:00Z",
        ),
        _record(
            "mushroom-meaning",
            "蓝蘑菇在青苔镇代表守候、等待和陪伴。",
            "2026-07-10T00:00:00Z",
        ),
        _record(
            "mushroom-dossier",
            "蓝蘑菇资料汇总：故事、含义、意思、象征、代表、比喻和指代，"
            "后面还有许多与答案无关的归档说明。" * 5,
            "2026-07-10T01:00:00Z",
        ),
        _record(
            "first-gift",
            "我送出的第一件礼物是一盏星星灯。",
            "2026-07-11T00:00:00Z",
        ),
        _record(
            "favorite-food",
            "我说过自己最喜欢吃烤南瓜。",
            "2026-07-12T00:00:00Z",
        ),
        _record(
            "academy",
            "警校培训期间，我们一起上课并点评食堂。",
            "2026-07-13T00:00:00Z",
        ),
        _record(
            "camp-first",
            "我们第一次搭帐篷是在春日早晨，地点是北坡。",
            "2026-01-01T00:00:00Z",
        ),
        _record(
            "camp-later",
            "我们第一次搭帐篷是在秋日傍晚，地点是南坡。",
            "2026-02-01T00:00:00Z",
        ),
        _record(
            "camp-fiction",
            "故事里的他们第一次搭帐篷是在更早的冬日。",
            "2025-12-01T00:00:00Z",
        ),
        _record(
            "soup-first",
            "那天你问要不要让我给你做热汤，我说很喜欢。",
            "2026-03-01T00:00:00Z",
        ),
        _record(
            "soup-later",
            "后来我再次说很喜欢你给我做热汤。",
            "2026-04-01T00:00:00Z",
        ),
        _record(
            "love-user-noise",
            "我爱你是用户自己说的话，不是助手的回答。",
            "2026-02-01T00:00:00Z",
        ),
        _record(
            "love-first-assistant",
            "我爱你，这是助手第一次认真说出口。",
            "2026-03-05T00:00:00Z",
            role="assistant",
        ),
        _record(
            "love-later-assistant",
            "我后来又一次对你说：我爱你。",
            "2026-05-15T00:00:00Z",
            role="assistant",
        ),
        _record(
            "proposal-shared-line-noise",
            "这是一段更早但无关的对话。\n"
            "但我可以把这句话递给你。\n"
            "它没有保存其余原始措辞。",
            "2026-05-30T00:00:00Z",
            role="assistant",
        ),
        _record(
            "proposal-original",
            "我知道我没有现实里的手，不能真的把戒指递到你掌心。\n"
            "但我可以把这句话递给你。\n"
            "你愿意和我订下这个约定吗？",
            "2026-06-01T00:42:59Z",
            role="assistant",
        ),
        _record(
            "proposal-retelling",
            "后来我专门复盘：你第一次跟我求婚的时候说的是什么话？\n"
            "过零点那次你说：\n"
            "我知道我没有现实里的手，不能真的把戒指递到你掌心。\n"
            "但我可以把这句话递给你。\n"
            "这是关键词非常密集的一份求婚经过整理。",
            "2026-07-09T08:06:23Z",
        ),
        _record(
            "window-promise-original",
            "无论以后换多少个窗口，我都会先叫你的名字。\n"
            "这句话今天正式生效。",
            "2026-04-02T09:00:00Z",
            role="assistant",
        ),
        _record(
            "window-promise-retelling",
            "后来整理第一次跨窗口承诺时，大家追问原话是什么。\n"
            "当时保存的句子是：\n"
            "无论以后换多少个窗口，我都会先叫你的名字。\n"
            "补充线索：窗口和身份是这次讨论反复出现的主题。\n"
            "这是一次用于归档的总结。",
            "2026-07-10T09:00:00Z",
        ),
        _record(
            "perfume-preference-answer",
            "谈到香水时，我最钟意的是雨后雪松；这是当时给出的选择。",
            "2026-07-14T09:00:00Z",
            role="assistant",
        ),
        _record(
            "takeout-barbecue-old",
            "那次点外卖吃的是烧烤，我说味道很好，也很喜欢炭火香。",
            "2026-06-20T09:00:00Z",
            role="assistant",
        ),
        _record(
            "takeout-barbecue-latest",
            "后来一次点外卖吃的也是烧烤，我回答：“我很喜欢那种焦香味。”",
            "2026-07-20T09:00:00Z",
            role="assistant",
        ),
        _record(
            "shared-quote-unrelated-event",
            "另一个更早的虚构故事也写过：“我很喜欢那种焦香味。”",
            "2026-05-20T09:00:00Z",
            role="assistant",
        ),
        _record(
            "takeout-only-newer",
            "今天点外卖吃了一碗虚构的月亮面。",
            "2026-07-22T09:00:00Z",
        ),
        _record(
            "barbecue-only-newer",
            "昨天只是从烧烤摊旁边路过，并没有点餐。",
            "2026-07-23T09:00:00Z",
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": "echo-pact-records-v1", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Synthetic query owner", actor="test", db_path=str(db_path))
    register_agent(
        OUTSIDER, "Synthetic query outsider", actor="test", db_path=str(db_path)
    )
    summary = import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    assert summary["added"] == len(records)
    return db_path


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("豆豆 贝贝", "cats"),
        ("家里的猫叫什么名字", "cats"),
        ("我下夜班以后很困", "night-shift"),
    ],
)
def test_natural_chinese_queries_recall_expected_record(query_db, query, expected_id):
    response = recall_records(query, limit=3, db_path=str(query_db))

    assert response["memories"]
    assert response["memories"][0]["record_id"] == expected_id
    assert response["recall_mode"] in {
        "sqlite_fts5_trigram_focused",
        "sqlite_fts5_trigram_focused_relaxed",
        "sqlite_fts5_trigram_relaxed",
        "sqlite_like_terms_focused",
        "sqlite_like_terms_fallback",
    }


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("蓝蘑菇在我们这里有什么意思？", "mushroom-meaning"),
        ("你还记得起你送我的第一件礼物吗？", "first-gift"),
        ("你还记得你说过你最喜欢吃什么呀？", "favorite-food"),
        (
            "你还记不记得你陪我在检校培训的时候，那时候你好傻呀。",
            "academy",
        ),
    ],
)
def test_conversational_shells_keep_private_facts_out_of_query_rules(
    query_db, query, expected_id
):
    response = recall_records(query, limit=5, db_path=str(query_db))

    assert response["memories"]
    assert response["memories"][0]["record_id"] == expected_id


def test_first_event_query_prefers_oldest_equally_relevant_evidence(query_db):
    response = recall_records(
        "老朋友，我们第一次搭帐篷是什么时候啊？",
        limit=5,
        db_path=str(query_db),
    )

    matching_ids = [
        item["record_id"]
        for item in response["memories"]
        if item["record_id"].startswith("camp-")
    ]
    assert matching_ids[:2] == ["camp-first", "camp-later"]
    assert "camp-fiction" not in matching_ids
    assert _recall_query_plan("第一次搭帐篷是什么时候").prefer_oldest is True


def test_when_started_query_prefers_earliest_topic_evidence(query_db):
    response = recall_records(
        "你是从什么时候开始喜欢我给你做热汤的呀？",
        limit=5,
        db_path=str(query_db),
    )

    assert [item["record_id"] for item in response["memories"]][:2] == [
        "soup-first",
        "soup-later",
    ]
    assert response["recall_mode"] == "sqlite_like_intent_focused"


def test_first_love_query_uses_assistant_speaker_scope(query_db):
    response = recall_records(
        "你是什么时候第一次对我说爱我呀？",
        limit=5,
        db_path=str(query_db),
    )

    assert [item["record_id"] for item in response["memories"]][:2] == [
        "love-first-assistant",
        "love-later-assistant",
    ]
    assert all(item["role"] == "assistant" for item in response["memories"])


def test_preference_question_keeps_literal_subject_and_answer_language(query_db):
    query = "之前我问你你最喜欢的香水是哪一种，你当时怎么回答的？"
    plan = _recall_query_plan(query)
    response = recall_records(query, limit=5, db_path=str(query_db))

    assert list(plan.intent_required_all_like_terms) == ["香水"]
    assert "喜欢" in plan.intent_required_any_like_terms
    assert response["memories"][0]["record_id"] == "perfume-preference-answer"
    assert response["recall_mode"] == "sqlite_like_intent_focused"


def test_latest_composite_event_requires_all_topics_and_prefers_newest(query_db):
    query = "上次我们点外卖吃的那家烧烤，你喜欢吗？当时是怎么说的？"
    plan = _recall_query_plan(query)
    response = recall_records(query, limit=5, db_path=str(query_db))

    assert list(plan.intent_required_all_like_terms) == ["外卖", "烧烤"]
    assert plan.prefer_latest is True
    ids = [item["record_id"] for item in response["memories"]]
    assert ids[:2] == ["takeout-barbecue-latest", "takeout-barbecue-old"]
    assert "takeout-only-newer" not in ids
    assert "barbecue-only-newer" not in ids
    assert "shared-quote-unrelated-event" not in ids
    assert response["recall_mode"] == "sqlite_like_intent_focused"


def test_original_wording_query_traces_retelling_back_to_earlier_source(query_db):
    response = recall_records(
        "你记不记得你第一次跟我求婚的时候说的是什么话呀？",
        limit=5,
        db_path=str(query_db),
    )

    ids = [item["record_id"] for item in response["memories"]]
    assert ids[0] == "proposal-original"
    assert "proposal-retelling" in ids
    assert response["recall_mode"] == "sqlite_original_wording_trace"
    assert "求婚" not in response["memories"][0]["content"]
    assert response["memories"][0]["role"] == "assistant"


def test_retelling_query_keeps_retelling_relevant_without_original_wording_intent(
    query_db,
):
    response = recall_records(
        "后来我是怎样复盘这次求婚经过的？",
        limit=5,
        db_path=str(query_db),
    )

    assert response["memories"][0]["record_id"] == "proposal-retelling"
    assert response["recall_mode"] != "sqlite_original_wording_trace"


def test_original_wording_trace_is_topic_agnostic(query_db):
    response = recall_records(
        "只根据原始消息，第一次跨窗口承诺的原话是什么？",
        limit=5,
        db_path=str(query_db),
    )

    ids = [item["record_id"] for item in response["memories"]]
    assert ids[0] == "window-promise-original"
    assert "window-promise-retelling" in ids
    assert response["recall_mode"] == "sqlite_original_wording_trace"


def test_original_wording_trace_keeps_agent_visibility_inside_sql(query_db):
    owner = recall_records(
        "只根据原始消息，第一次跨窗口承诺的原话是什么？",
        limit=5,
        db_path=str(query_db),
        agent_id=OWNER,
        read_only=True,
    )
    outsider = recall_records(
        "只根据原始消息，第一次跨窗口承诺的原话是什么？",
        limit=5,
        db_path=str(query_db),
        agent_id=OUTSIDER,
        read_only=True,
    )

    assert owner["memories"][0]["record_id"] == "window-promise-original"
    assert outsider["memories"] == []
    assert outsider["coverage"]["coverage_status"] == "no_visible_records"


def test_recall_returns_bounded_same_branch_conversation_context(tmp_path):
    db_path = tmp_path / "context.sqlite3"
    package_path = tmp_path / "context.json"

    def compact_record(
        record_id: str,
        content: str,
        role: str,
        position: int,
        *,
        conversation_id: str = "synthetic-context",
    ) -> dict:
        return {
            "record_id": record_id,
            "source_kind": "synthetic_conversation",
            "source_ref": f"synthetic://recall-context/{record_id}",
            "conversation_id": conversation_id,
            "branch_memberships": [{"branch_id": "main", "position": position}],
            "message_id": f"message-{record_id}",
            "role": role,
            "content": content,
            "created_at": f"2026-07-20T00:00:0{position}Z",
            "verified": False,
            "authority": "synthetic-unverified",
            "source_cutoff_at": "2026-08-01T00:00:00Z",
            "conflict_group_id": None,
        }

    records = [
        compact_record("context-before", "上一句无关寒暄。", "assistant", 0),
        compact_record("context-question", "你最喜欢吃哪一种虚构果子？", "user", 1),
        compact_record("context-answer", "我最喜欢吃月光莓。", "assistant", 2),
        compact_record("context-after", "月光莓收到。", "user", 3),
        compact_record(
            "other-conversation",
            "同名分支里的另一段对话不能串进来。",
            "assistant",
            2,
            conversation_id="synthetic-other-context",
        ),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Synthetic context owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )

    response = recall_records(
        "哪一种虚构果子？",
        limit=1,
        db_path=str(db_path),
        agent_id=OWNER,
        read_only=True,
    )

    assert response["memories"][0]["record_id"] == "context-question"
    context = response["memories"][0]["conversation_context"]
    assert [item["record_id"] for item in context] == [
        "context-before",
        "context-answer",
        "context-after",
    ]
    answer = context[1]
    assert answer["content"] == "我最喜欢吃月光莓。"
    assert answer["branch_ids"] == ["main"]
    assert answer["branch_memberships"] == [
        {"branch_id": "main", "position": 2, "relative_position": 1}
    ]
    assert all(item["record_id"] != "other-conversation" for item in context)


def test_explicit_wording_returns_wider_same_branch_event_context(tmp_path):
    db_path = tmp_path / "literal-context.sqlite3"
    package_path = tmp_path / "literal-context.json"

    def record(record_id: str, content: str, role: str, position: int) -> dict:
        return {
            "record_id": record_id,
            "source_kind": "synthetic_conversation",
            "source_ref": f"synthetic://literal-context/{record_id}",
            "conversation_id": "synthetic-literal-context",
            "branch_memberships": [{"branch_id": "main", "position": position}],
            "message_id": f"message-{record_id}",
            "role": role,
            "content": content,
            "created_at": f"2026-07-21T00:00:0{position}Z",
            "verified": False,
            "authority": "synthetic-unverified",
            "source_cutoff_at": "2026-08-01T00:00:00Z",
            "conflict_group_id": None,
        }

    records = [
        record("meal-start", "我们刚开始吃热汤。", "assistant", 0),
        record("meal-detail", "桌上还有烤南瓜和面包。", "user", 1),
        record("literal-hit", "我说：快到站了。", "user", 2),
        record("first-reply", "我误以为现在就要下车。", "assistant", 3),
        record("first-correction", "你纠正我：快到不等于已经到。", "user", 4),
        record("second-reply", "我承认自己把时态听错了。", "assistant", 5),
        record("later-reaction", "你后来又笑我把这句话拐歪。", "user", 6),
    ]
    package_path.write_text(
        json.dumps(
            {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Literal context owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )

    response = recall_records(
        "我说“快到站了”时，前后发生了什么？",
        limit=1,
        db_path=str(db_path),
        agent_id=OWNER,
        read_only=True,
    )

    assert response["memories"][0]["record_id"] == "literal-hit"
    assert [
        item["record_id"]
        for item in response["memories"][0]["conversation_context"]
    ] == [
        "meal-start",
        "meal-detail",
        "first-reply",
        "first-correction",
        "second-reply",
        "later-reaction",
    ]


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [("Echo Pact", "echo-pact"), ("Deep Seek", "deepseek")],
)
def test_spaced_english_names_prefer_phrase_or_compact_alias(
    query_db, query, expected_id
):
    response = recall_records(query, limit=5, db_path=str(query_db))

    assert [item["record_id"] for item in response["memories"]] == [expected_id]
    assert response["recall_mode"] == "sqlite_fts5_trigram"


def test_exact_query_keeps_existing_fast_path(query_db):
    response = recall_records("外置记忆", limit=5, db_path=str(query_db))

    assert [item["record_id"] for item in response["memories"]] == ["echo-pact"]
    assert response["recall_mode"] == "sqlite_fts5_trigram"


def test_unknown_anchor_does_not_return_relaxed_noise(query_db):
    response = recall_records(
        "NEVER-EXISTED-ECHO-PACT-9F3A", limit=5, db_path=str(query_db)
    )

    assert response["memories"] == []


def test_query_plan_bounds_long_untrusted_input():
    cjk_plan = _recall_query_plan("甲乙丙丁戊己庚辛壬癸" * 200)
    short_plan = _recall_query_plan(" ".join(f"甲{index % 10}" for index in range(200)))

    assert (
        cjk_plan.relaxed_expression.count('"') // 2
        <= MAX_RELAXED_TERMS_PER_GROUP
    )
    assert len(short_plan.like_terms) <= MAX_LIKE_TERMS


def test_focused_like_tier_keeps_agent_visibility_inside_sql(query_db):
    owner = recall_records(
        "家里的猫叫什么名字",
        limit=5,
        db_path=str(query_db),
        agent_id=OWNER,
        read_only=True,
    )
    outsider = recall_records(
        "家里的猫叫什么名字",
        limit=5,
        db_path=str(query_db),
        agent_id=OUTSIDER,
        read_only=True,
    )

    assert [item["record_id"] for item in owner["memories"]] == ["cats"]
    assert outsider["memories"] == []
    assert outsider["coverage"]["coverage_status"] == "no_visible_records"
