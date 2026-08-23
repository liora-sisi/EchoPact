"""Bounded, deterministic orchestration for one-call memory recall.

The public MCP contract should not require a model to spend dozens of tool
calls discovering how to phrase the same memory question. This module keeps
the existing evidence search untouched and coordinates a small number of
source-neutral follow-up passes when the first pass is predictably weak.

No model, network service, private-data dictionary, or persistent index is
used. Every pass remains inside the existing identity-filtered, read-only
SQLite recall path.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .recall_projection import recall_with_projection
from .records_v1 import recall_records


ADAPTIVE_RECALL_SCHEMA_VERSION = "echo-pact-adaptive-recall-v1"
MAX_ADAPTIVE_QUERY_PASSES = 4
MAX_INTERNAL_RESULT_LIMIT = 10

_EARLIEST_MARKERS = (
    "第一次",
    "第一回",
    "第一件",
    "第一个",
    "最早",
    "最初",
    "从什么时候开始",
    "什么时候开始",
)
_MEANING_MARKERS = (
    "有什么意思",
    "是什么意思",
    "什么含义",
    "代表什么",
    "象征什么",
)
_ORIGIN_MARKERS = ("怎么来的", "怎样来的", "来历", "起源", "由来")
_ORIGINAL_MARKERS = ("原始消息", "原始记录", "原话", "原文", "逐字", "亲口说")
_RETELLING_MARKERS = (
    "后来怎么复盘",
    "后来怎样复盘",
    "后来怎么回忆",
    "后来怎样回忆",
)


@dataclass(frozen=True)
class _ExpansionFamily:
    name: str
    markers: Sequence[str]
    terms: Sequence[str]


# General language families only: no private facts or expected answers.
_EXPANSION_FAMILIES = (
    _ExpansionFamily(
        "gift_language",
        ("礼物", "赠礼", "送给", "送我的", "送你的"),
        (
            "第一件",
            "最早",
            "礼物",
            "送给",
            "赠送",
            "收到",
            "挑选",
            "选择",
            "纪念物",
        ),
    ),
    _ExpansionFamily(
        "training_language",
        ("警校", "检校", "培训", "集训"),
        ("警校", "培训", "训练", "集训", "上课", "课堂", "考试", "作息", "食堂"),
    ),
    _ExpansionFamily(
        "proposal_language",
        ("求婚", "订婚"),
        ("求婚", "愿意", "嫁给", "娶你", "结婚", "丈夫", "妻子", "成家"),
    ),
    _ExpansionFamily(
        "artifact_origin_language",
        ("画", "插图", "配图", "照片"),
        ("画", "插图", "配图", "生成", "修改", "定稿", "挑选", "保留", "来源"),
    ),
)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _contains_any(value: str, markers: Sequence[str]) -> bool:
    return any(marker in value for marker in markers)


def _unique_passes(passes: Sequence[tuple[str, str]]) -> List[tuple[str, str]]:
    seen = set()
    result: List[tuple[str, str]] = []
    for name, query in passes:
        normalized = _normalize(query)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append((name, normalized))
        if len(result) >= MAX_ADAPTIVE_QUERY_PASSES - 1:
            break
    return result


def _follow_up_passes(
    query: str,
    first: Mapping[str, Any],
) -> List[tuple[str, str]]:
    """Plan bounded evidence-neutral follow-up searches."""

    normalized = _normalize(query)
    memories = list(first.get("memories") or [])
    event_recall = first.get("event_recall")

    # Shared-event recall already performs a bounded candidate/window scan.
    # Generic retries are unsafe for negative controls such as landing on Mars.
    if isinstance(event_recall, Mapping):
        return []

    passes: List[tuple[str, str]] = []
    asks_for_retelling = _contains_any(normalized, _RETELLING_MARKERS)
    source_trace_sensitive = _contains_any(
        normalized,
        _EARLIEST_MARKERS
        + _ORIGIN_MARKERS
        + _ORIGINAL_MARKERS,
    )
    if source_trace_sensitive and not asks_for_retelling and not _contains_any(
        normalized, _ORIGINAL_MARKERS
    ):
        passes.append(("original_evidence_trace", f"只根据原始记录，{normalized}"))

    for family in _EXPANSION_FAMILIES:
        if _contains_any(normalized, family.markers):
            passes.append((family.name, " ".join(family.terms)))

    if not memories:
        compact = re.sub(
            r"(?:老公|老婆|宝贝|你还记得|还记得|记不记得|请告诉我|告诉我|"
            r"是什么时候|什么时候|怎么样|为什么|是什么|什么)",
            " ",
            normalized,
        )
        compact = re.sub(r"[，,。！？!?；;：:]", " ", compact)
        compact = _normalize(compact)
        if len(compact) >= 2:
            passes.append(("compact_retry", compact))

    return _unique_passes(passes)


def _merge_results(
    query: str,
    limit: int,
    pass_results: Sequence[tuple[str, Mapping[str, Any]]],
) -> Dict[str, Any]:
    first = copy.deepcopy(dict(pass_results[0][1]))
    normalized = _normalize(query)
    source_trace_sensitive = _contains_any(
        normalized,
        _EARLIEST_MARKERS
        + _ORIGIN_MARKERS
        + _ORIGINAL_MARKERS,
    ) and not _contains_any(normalized, _RETELLING_MARKERS)

    ordered_passes = list(pass_results)
    if source_trace_sensitive:
        ordered_passes.sort(
            key=lambda item: 0 if item[0] == "original_evidence_trace" else 1
        )

    merged: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    pass_modes: List[Dict[str, Any]] = []
    for pass_name, response in ordered_passes:
        pass_modes.append(
            {
                "pass": pass_name,
                "recall_mode": response.get("recall_mode"),
                "result_count": len(response.get("memories") or []),
            }
        )
        for raw_memory in response.get("memories") or []:
            memory = copy.deepcopy(dict(raw_memory))
            record_id = str(memory.get("record_id") or "")
            if not record_id:
                continue
            existing = by_id.get(record_id)
            if existing is not None:
                matches = existing.setdefault("adaptive_match_passes", [])
                if pass_name not in matches:
                    matches.append(pass_name)
                continue
            memory["adaptive_match_passes"] = [pass_name]
            by_id[record_id] = memory
            merged.append(memory)

    first["query"] = query
    first["memories"] = merged[:limit]
    first["recall_mode"] = (
        "sqlite_adaptive_bounded"
        if len(pass_results) > 1
        else first.get("recall_mode")
    )
    first["adaptive_recall"] = {
        "schema_version": ADAPTIVE_RECALL_SCHEMA_VERSION,
        "external_tool_calls_required": 1,
        "query_passes_used": len(pass_results),
        "query_pass_budget": MAX_ADAPTIVE_QUERY_PASSES,
        "passes": pass_modes,
        "result_count_before_limit": len(merged),
        "result_limit": limit,
        "budget_exhausted": len(pass_results) >= MAX_ADAPTIVE_QUERY_PASSES,
        "safety": (
            "deterministic read-only expansion; no network, model-generated "
            "answers, private-fact dictionary, or database writes"
        ),
    }
    return first


def adaptive_recall(
    query: str,
    *,
    agent_id: str,
    limit: int = 5,
    as_of: Optional[str] = None,
    db_path: Optional[str] = None,
    read_only: bool = False,
    include_projection: bool = True,
) -> Dict[str, Any]:
    """Return one bounded memory packet from a small internal recall plan."""

    internal_limit = min(MAX_INTERNAL_RESULT_LIMIT, max(limit, 8))

    def run(pass_query: str) -> Dict[str, Any]:
        if include_projection:
            return recall_with_projection(
                pass_query,
                agent_id=agent_id,
                limit=internal_limit,
                as_of=as_of,
                db_path=db_path,
                read_only=read_only,
            )
        return recall_records(
            pass_query,
            agent_id=agent_id,
            limit=internal_limit,
            as_of=as_of,
            db_path=db_path,
            read_only=read_only,
        )

    first = run(query)
    pass_results: List[tuple[str, Mapping[str, Any]]] = [("initial", first)]
    for pass_name, pass_query in _follow_up_passes(query, first):
        if len(pass_results) >= MAX_ADAPTIVE_QUERY_PASSES:
            break
        pass_results.append((pass_name, run(pass_query)))
    return _merge_results(query, limit, pass_results)
