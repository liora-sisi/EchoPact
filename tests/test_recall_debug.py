"""Synthetic acceptance for the local M14 recall debug panel."""

from __future__ import annotations

import json
from pathlib import Path

from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory.identity import register_agent
from backend.memory.recall_debug import render_recall_debug, run_recall_debug
from backend.memory.records_v1 import import_record_package
from backend.memory.speech_query_rescue import (
    SpeechQueryMapping,
    SpeechQueryRescuePolicy,
)


OWNER = "agt-recall-debug-owner"


def _record(
    record_id: str,
    *,
    source_kind: str,
    content: str,
    created_at: str,
) -> dict:
    authority = (
        "room-ferry-unverified-backup"
        if source_kind == "room-ferry-backup-v1"
        else "codex-local-unverified-archive"
    )
    return {
        "record_id": record_id,
        "source_kind": source_kind,
        "source_ref": f"synthetic://debug/{record_id}",
        "conversation_id": f"conversation-{source_kind}",
        "branch_id": "main",
        "message_id": f"message-{record_id}",
        "role": "user",
        "content": content,
        "created_at": created_at,
        "verified": False,
        "authority": authority,
        "source_cutoff_at": "2026-09-16T00:00:00Z",
        "conflict_group_id": None,
    }


def _database(tmp_path: Path, records: list[dict]) -> Path:
    db_path = tmp_path / "debug.sqlite3"
    package_path = tmp_path / "records.json"
    package_path.write_text(
        json.dumps(
            {"schema_version": "echo-pact-records-v1", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_agent(OWNER, "Debug owner", actor="test", db_path=str(db_path))
    import_record_package(
        str(package_path),
        db_path=str(db_path),
        owner_agent_id=OWNER,
        actor="test",
    )
    return db_path


def test_debug_panel_labels_sources_and_explains_mirror_compaction(tmp_path):
    mirror = "M14-TRACE-4411 蓝色航标灯放在窗边，明早一起核对潮汐刻度。"
    db_path = _database(
        tmp_path,
        [
            _record(
                "chatgpt-mirror",
                source_kind="room-ferry-backup-v1",
                content=mirror,
                created_at="2026-09-15T01:00:00Z",
            ),
            _record(
                "codex-mirror",
                source_kind="codex-app-server-v1",
                content=mirror,
                created_at="2026-09-15T01:00:00Z",
            ),
            _record(
                "codex-distinct",
                source_kind="codex-app-server-v1",
                content="M14-TRACE-4411 另一条独立的召回调试证据。",
                created_at="2026-09-15T02:00:00Z",
            ),
        ],
    )

    response = run_recall_debug(
        "M14-TRACE-4411",
        db_path=str(db_path),
        agent_id=OWNER,
        limit=2,
        include_projection=False,
    )

    debug = response["recall_debug"]
    assert debug["engine"]["legacy_weight_formula_used"] is False
    assert {item["source_channel"] for item in debug["top_k"]} == {
        "chatgpt",
        "codex_local",
    }
    compacted = [
        item
        for item in debug["exclusions"]
        if item["reason_code"] == "exact_mirror_compacted"
    ]
    assert len(compacted) == 1
    assert compacted[0]["merged_into_record_id"] in {
        "chatgpt-mirror",
        "codex-mirror",
    }
    representative_position = compacted[0]["merged_into_position"]
    assert representative_position in {1, 2}
    assert debug["top_k"][representative_position - 1]["record_id"] == (
        compacted[0]["merged_into_record_id"]
    )
    assert debug["passes"][0]["query"] == "M14-TRACE-4411"
    assert debug["top_k"][0]["retrieval_debug"]["final_weight"] is None

    rendered = render_recall_debug(response)
    assert "GPT库" in rendered
    assert "Codex库" in rendered
    assert "镜像收敛" in rendered
    assert f"合并到第 {representative_position} 条" in rendered
    assert "不使用旧 semantic/emotion/importance/decay/saga" in rendered


def test_debug_panel_shows_original_and_m8_candidate_query(tmp_path):
    db_path = _database(
        tmp_path,
        [
            _record(
                "codex-wrist",
                source_kind="codex-app-server-v1",
                content="我最近手腕怎么样：已经不再发涩，转动时舒服多了。",
                created_at="2026-09-15T03:00:00Z",
            )
        ],
    )
    policy = SpeechQueryRescuePolicy(
        mappings=(SpeechQueryMapping("手完", "手腕"),)
    )

    response = run_recall_debug(
        "手完",
        db_path=str(db_path),
        agent_id=OWNER,
        limit=1,
        include_projection=False,
        speech_query_policy=policy,
    )

    speech = response["speech_query_rescue"]
    assert speech["original_query"] == "手完"
    assert speech["candidates"][0] == {
        "pass": "speech_query_rescue_1",
        "observed": "手完",
        "intended": "手腕",
        "candidate_query": "手腕",
    }
    rendered = render_recall_debug(response)
    assert "原串：手完" in rendered
    assert "候补：手腕（手完 → 手腕）" in rendered
    assert "Codex库" in rendered


def test_debug_panel_distinguishes_official_and_browser_chatgpt_sources(tmp_path):
    db_path = _database(
        tmp_path,
        [
            _record(
                "official-gpt",
                source_kind="room-ferry-backup-v1",
                content="M14-SOURCE-7711 官方数据包中的合成记录。",
                created_at="2026-09-15T03:00:00Z",
            ),
            _record(
                "browser-gpt",
                source_kind="guijian-browser-capture-v1",
                content="M14-SOURCE-7711 浏览器抓取中的合成记录。",
                created_at="2026-09-15T03:01:00Z",
            ),
        ],
    )

    response = run_recall_debug(
        "M14-SOURCE-7711",
        db_path=str(db_path),
        agent_id=OWNER,
        limit=2,
        include_projection=False,
    )
    rendered = render_recall_debug(response)

    assert "GPT库（官方数据包）" in rendered
    assert "GPT库（浏览器抓取）" in rendered


def test_normal_gateway_never_exposes_local_debug_trace(tmp_path):
    db_path = _database(
        tmp_path,
        [
            _record(
                "codex-normal",
                source_kind="codex-app-server-v1",
                content="M14-NORMAL-9922 普通只读调用不带调试内脏。",
                created_at="2026-09-15T04:00:00Z",
            )
        ],
    )

    response = ReadonlyGateway(str(db_path), OWNER).recall(
        {
            "query": "M14-NORMAL-9922",
            "limit": 1,
            "include_projection": False,
        }
    )

    assert "recall_debug" not in response
    assert "recall_debug_pass" not in response
    assert "retrieval_debug" not in response["memories"][0]


def test_debug_panel_explains_candidates_outside_top_k(tmp_path):
    db_path = _database(
        tmp_path,
        [
            _record(
                f"codex-limit-{index}",
                source_kind="codex-app-server-v1",
                content=f"M14-LIMIT-7712 第 {index} 条独立候选证据。",
                created_at=f"2026-09-15T0{index}:00:00Z",
            )
            for index in range(1, 4)
        ],
    )

    response = run_recall_debug(
        "M14-LIMIT-7712",
        db_path=str(db_path),
        agent_id=OWNER,
        limit=1,
        include_projection=False,
    )

    assert len(response["recall_debug"]["top_k"]) == 1
    excluded = [
        item
        for item in response["recall_debug"]["exclusions"]
        if item["reason_code"] == "result_limit"
    ]
    assert len(excluded) == 2
    assert all(item["source_channel"] == "codex_local" for item in excluded)
    assert "排在本次 Top-K 之外" in render_recall_debug(response)
