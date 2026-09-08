"""Synthetic-only checks for bounded speech-to-text query rescue."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package
from backend.memory.speech_query_rescue import (
    SpeechQueryMapping,
    SpeechQueryRescuePolicy,
    plan_speech_query_rescue,
)


OWNER = "agt-speech-rescue-owner"
AS_OF = "2026-09-08T06:00:00Z"

RESCUE_MAPPINGS = (
    SpeechQueryMapping("篮莓塔", "蓝莓塔"),
    SpeechQueryMapping("薄荷气泡氺", "薄荷气泡水"),
    SpeechQueryMapping("纸鸳节", "纸鸢节"),
    SpeechQueryMapping("萤伙虫夜游", "萤火虫夜游"),
    SpeechQueryMapping("陶磁杯", "陶瓷杯"),
    SpeechQueryMapping("风玲桥", "风铃桥"),
    SpeechQueryMapping("树梅酱", "树莓酱"),
    SpeechQueryMapping("玻离花房", "玻璃花房"),
)
PROTECTED_MAPPINGS = (
    SpeechQueryMapping("星兰", "星岚"),
    SpeechQueryMapping("舟登", "舟灯"),
    SpeechQueryMapping("山响", "山想"),
    SpeechQueryMapping("雾雀", "雾鹊"),
    SpeechQueryMapping("摆龙门镇", "摆龙门阵"),
)
PROTECTED_TERMS = tuple(item.observed for item in PROTECTED_MAPPINGS)
POLICY = SpeechQueryRescuePolicy(
    mappings=RESCUE_MAPPINGS + PROTECTED_MAPPINGS,
    protected_terms=PROTECTED_TERMS,
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(record_id: str, content: str) -> dict:
    return {
        "record_id": record_id,
        "source_kind": "synthetic_conversation",
        "source_ref": f"synthetic://speech-rescue/{record_id}",
        "conversation_id": "synthetic-speech-rescue",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": "user",
        "content": content,
        "created_at": "2026-09-01T00:00:00Z",
        "verified": False,
        "authority": "synthetic-unverified",
        "source_cutoff_at": "2026-09-02T00:00:00Z",
        "conflict_group_id": None,
    }


def test_twenty_case_acceptance_matrix_has_zero_false_corrections():
    cases = [
        ("上次湖边吃的篮莓塔", "rescue", "蓝莓塔"),
        ("我们喝过薄荷气泡氺吗", "rescue", "薄荷气泡水"),
        ("纸鸳节是哪一天", "rescue", "纸鸢节"),
        ("记得那次萤伙虫夜游吗", "rescue", "萤火虫夜游"),
        ("蓝釉陶磁杯放在哪里", "rescue", "陶瓷杯"),
        ("风玲桥那张照片是哪次", "rescue", "风铃桥"),
        ("早餐的树梅酱是谁选的", "rescue", "树莓酱"),
        ("玻离花房后来去成了吗", "rescue", "玻璃花房"),
        ("星兰第一次出现在哪里", "protected", None),
        ("舟登这个昵称怎么来的", "protected", None),
        ("山响那首歌是谁选的", "protected", None),
        ("雾雀计划后来完成了吗", "protected", None),
        ("我们那晚摆龙门镇说了什么", "protected", None),
        ("好好好，你说得都对", "unchanged", None),
        ("这可真是太棒了呢", "unchanged", None),
        ("巴适得板那顿饭", "unchanged", None),
        ("安逸得很的周末", "unchanged", None),
        ("[图片]里是什么", "image", None),
        ("[Image] 这张有原始文字吗", "image", None),
        ("<图片占位符>能证明颜色吗", "image", None),
    ]
    assert len(cases) == 20

    rescued = 0
    false_corrections = 0
    false_triggers = 0
    for query, category, expected_term in cases:
        passes, trace = plan_speech_query_rescue(
            query, {"memories": []}, POLICY
        )
        if category == "rescue":
            assert trace["triggered"] is True
            assert expected_term in passes[0][1]
            rescued += 1
        else:
            if passes:
                false_corrections += 1
            if trace["triggered"]:
                false_triggers += 1
            assert passes == []
            if category == "protected":
                assert trace["trigger_reason"] == "protected_term_match"

    assert rescued == 8
    assert false_corrections == 0
    assert false_triggers == 0


def test_initial_evidence_for_observed_term_blocks_hidden_correction():
    initial = {
        "memories": [
            {
                "record_id": "literal-observed-form",
                "content": "店名本来就写作篮莓塔，不是录音错字。",
            }
        ]
    }

    passes, trace = plan_speech_query_rescue(
        "篮莓塔是哪家店", initial, POLICY
    )

    assert passes == []
    assert trace["triggered"] is False
    assert trace["trigger_reason"] == "initial_evidence_supports_observed_term"


def test_initial_evidence_for_intended_term_does_not_spend_a_retry():
    initial = {
        "memories": [
            {
                "record_id": "already-found-intended-form",
                "content": "湖边小店的蓝莓塔用了浅蓝色盘子。",
            }
        ]
    }

    passes, trace = plan_speech_query_rescue(
        "上次湖边吃的篮莓塔", initial, POLICY
    )

    assert passes == []
    assert trace["triggered"] is False
    assert trace["trigger_reason"] == "initial_evidence_supports_intended_term"


def test_gateway_rescues_weak_nonempty_hit_and_keeps_database_read_only(
    tmp_path: Path,
):
    db_path = tmp_path / "speech-rescue.sqlite3"
    package_path = tmp_path / "speech-rescue.json"
    package_path.write_text(
        json.dumps(
            {
                "schema_version": "echo-pact-records-v1",
                "records": [
                    _record(
                        "blueberry-tart",
                        "湖边小店的蓝莓塔用了浅蓝色盘子。",
                    ),
                    *[
                        _record(
                            f"lake-noise-{index}",
                            f"上次在湖边吃饭只讨论了天气，没有点心。编号{index}",
                        )
                        for index in range(10)
                    ],
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(
        OWNER, "Speech rescue owner", actor="test", db_path=str(db_path)
    )
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    before = _hash(db_path)

    response = ReadonlyGateway(
        str(db_path), OWNER, speech_query_policy=POLICY
    ).recall(
        {
            "query": "上次湖边吃的篮莓塔",
            "limit": 1,
            "as_of": AS_OF,
        }
    )

    assert _hash(db_path) == before
    assert response["query"] == "上次湖边吃的篮莓塔"
    assert response["speech_query_rescue"]["triggered"] is True
    assert response["speech_query_rescue"]["trigger_reason"] in {
        "no_results",
        "weak_unanchored_results",
    }
    assert response["speech_query_rescue"]["candidates"] == [
        {
            "pass": "speech_query_rescue_1",
            "observed": "篮莓塔",
            "intended": "蓝莓塔",
            "candidate_query": "上次湖边吃的蓝莓塔",
        }
    ]
    assert response["speech_query_rescue"]["passes"][0]["result_count"] >= 1
    assert [item["record_id"] for item in response["memories"]] == [
        "blueberry-tart"
    ]
    assert {
        item["record_id"]
        for item in response["speech_query_rescue"]["selected_hits"]
    } == {"blueberry-tart"}
