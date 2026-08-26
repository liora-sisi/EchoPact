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
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .event_collection import (
    MAX_COLLECTION_INTERNAL_RESULT_LIMIT,
    build_event_collection,
    plan_event_collection_passes,
)
from .event_timeline import (
    build_event_timeline,
    resolve_query_calendar_scope,
    suppress_event_timeline_for_coverage_gap,
)
from .recall_projection import recall_with_projection
from .records_v1 import _explicit_query_anchors, recall_records


ADAPTIVE_RECALL_SCHEMA_VERSION = "echo-pact-adaptive-recall-v1"
TEMPORAL_COVERAGE_SCHEMA_VERSION = "echo-pact-query-time-coverage-v1"
MAX_ADAPTIVE_QUERY_PASSES = 4
MAX_INTERNAL_RESULT_LIMIT = 10
MAX_EXPLICIT_SUBQUESTIONS = 3

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
_RETELLING_EVIDENCE_MARKERS = (
    "后来",
    "回忆",
    "复盘",
    "提起",
    "重述",
    "复述",
    "再说起",
)
_GENERIC_MEAL_QUERY_MARKERS = (
    "吃东西",
    "吃饭",
    "吃了什么",
    "吃的什么",
)
_GENERIC_MEAL_RESCUE_QUERY = (
    "我们第一次一起吃饭 吃东西 食物 早餐 午饭 晚饭 夜宵 "
    "烧烤 火锅 外卖 零食 水果"
)
_NAME_ORIGIN_MARKERS = (
    "为什么叫",
    "名字怎么",
    "名字是怎么",
    "名字由来",
    "这个名字",
    "取名",
    "命名",
    "叫法",
)
_RELATIVE_SCOPE_MARKERS = (
    "最近一个月",
    "过去一个月",
    "上个月",
    "上星期",
    "上周",
    "前天",
    "昨天",
    "昨晚",
    "今天",
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


def _explicit_subquestions(query: str) -> List[str]:
    """Split only caller-written question boundaries, outside quoted text.

    This is deliberately narrower than sentence segmentation. Commas and
    natural-language conjunctions are left untouched so the recall layer does
    not invent a task decomposition the caller did not express. Question
    marks and semicolons provide an auditable boundary for a small number of
    deterministic internal passes.
    """

    normalized = _normalize(query)
    parts: List[str] = []
    current: List[str] = []
    quote_end: Optional[str] = None
    quote_pairs = {"“": "”", '"': '"', "'": "'", "《": "》"}
    for char in normalized:
        if quote_end is not None:
            current.append(char)
            if char == quote_end:
                quote_end = None
            continue
        if char in quote_pairs:
            quote_end = quote_pairs[char]
            current.append(char)
            continue
        if char in "？?；;":
            value = _normalize("".join(current).strip("，,。！!：: "))
            if value:
                parts.append(value)
            current = []
            continue
        current.append(char)
    value = _normalize("".join(current).strip("，,。！!：: "))
    if value:
        parts.append(value)

    unique: List[str] = []
    seen = set()
    for part in parts:
        key = part.casefold()
        if len(part) < 3 or key in seen:
            continue
        seen.add(key)
        unique.append(part)
        if len(unique) >= MAX_EXPLICIT_SUBQUESTIONS:
            break
    return unique if len(unique) >= 2 else []


def _subquestion_subject_hint(first_question: str) -> Optional[str]:
    """Keep a bounded literal subject for dependent later questions."""

    value = re.sub(
        r"^(?:老公|老婆|宝贝|大宝贝|你还记得|还记得|记不记得)\s*",
        "",
        _normalize(first_question),
    )
    marker_positions = [
        position
        for marker in (
            "是什么",
            "为什么",
            "怎么样",
            "怎样",
            "怎么",
            "如何",
            "什么时候",
            "何时",
            "哪里",
            "哪儿",
            "是否",
            "有没有",
            "谁",
            "多少",
        )
        if (position := value.find(marker)) >= 2
    ]
    if marker_positions:
        value = value[: min(marker_positions)]
    value = re.sub(
        r"(?:最初|最后|当时|后来|原计划去?|计划去?|想|要|是在|在|是|的)+$",
        "",
        value.strip("，,。！？!?；;：: "),
    ).strip()
    value = re.sub(
        r"的?(?:几个|不同|这些|那些)?"
        r"(?:版本|方案|款式|候选|选项|记录|说法|阶段)$",
        "",
        value,
    ).strip()
    if not 2 <= len(value) <= 48:
        return None
    if len(re.findall(r"[0-9A-Za-z\u3400-\u9fff]", value)) < 2:
        return None
    if value in {
        "这件事",
        "那件事",
        "这个事",
        "那个事",
        "这次",
        "那次",
        "当时",
        "后来",
        "我们",
    }:
        return None
    return value


def _subquestion_passes(query: str) -> List[tuple[str, str]]:
    """Return bounded passes for an explicitly multi-part user question."""

    questions = _explicit_subquestions(query)
    if not questions:
        return []
    subject_hint = _subquestion_subject_hint(questions[0])
    dependent_markers = (
        "后来",
        "之后",
        "再后来",
        "当时",
        "那时",
        "那天",
        "最后",
        "结果",
        "实际",
        "接着",
        "随后",
        "然后",
    )
    result: List[tuple[str, str]] = []
    has_independent_topic = False
    # The initial pass already represents the first question. Add a second
    # pass only when it is more focused than repeating the full prompt:
    # dependent wording gets the first question's literal subject, while a
    # genuinely independent question must expose its own non-pronominal
    # subject. Broad clauses such as "why did I move" stay with the initial
    # evidence instead of consuming result slots with an unanchored search.
    for index, question in enumerate(questions[1:], start=2):
        if question.startswith(dependent_markers):
            if subject_hint:
                result.append((f"subquestion_{index}_subject", subject_hint))
            continue
        own_subject = _subquestion_subject_hint(question)
        if (
            own_subject
            and len(own_subject) >= 4
            and not own_subject.startswith(
                ("我", "你", "他", "她", "它", "这", "那", "当时", "后来")
            )
        ):
            result.append((f"subquestion_{index}", question))
            has_independent_topic = True
    if has_independent_topic:
        result.insert(0, ("subquestion_1", questions[0]))
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
    # Once the event window itself qualifies, one narrow pass may look for
    # explicit later retellings; an unsupported event still fails closed.
    if isinstance(event_recall, Mapping):
        passes: List[tuple[str, str]] = []
        if _contains_any(normalized, _GENERIC_MEAL_QUERY_MARKERS):
            passes.append(("shared_event_food_trace", _GENERIC_MEAL_RESCUE_QUERY))
        if event_recall.get("status") not in {
            "partial_support",
            "earliest_supported_candidate",
        }:
            return _unique_passes(passes)
        subject = _subquestion_subject_hint(normalized) or normalized
        for marker in _EARLIEST_MARKERS + ("当时",):
            subject = subject.replace(marker, "")
        subject = _normalize(subject.strip("，,。！？!?；;：: "))
        if len(subject) >= 2:
            passes.append(
                (
                    "event_retelling_trace",
                    f"{subject} 后来回忆 后来复盘 后来提起",
                )
            )
        return _unique_passes(passes)

    collection_intent, collection_passes = plan_event_collection_passes(normalized)
    if collection_intent is not None:
        return _unique_passes(collection_passes)

    passes: List[tuple[str, str]] = _subquestion_passes(normalized)
    literal_anchors = _explicit_query_anchors(normalized)
    name_subject = _name_origin_subject(normalized)
    if name_subject:
        passes.append(
            (
                "name_origin_language",
                f"{name_subject} 名字 取名 命名 叫法 由来 最初 当时",
            )
        )
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

    if not literal_anchors:
        for family in _EXPANSION_FAMILIES:
            if _contains_any(normalized, family.markers):
                passes.append((family.name, " ".join(family.terms)))

    if not memories:
        for index, anchor in enumerate(literal_anchors, start=1):
            passes.append((f"literal_anchor_{index}", anchor))
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


def _name_origin_subject(query: str) -> Optional[str]:
    """Extract only the caller-written name for a generic origin rescue."""

    normalized = _normalize(query)
    if not _contains_any(normalized, _NAME_ORIGIN_MARKERS):
        return None
    candidates: List[str] = []
    candidates.extend(
        match.group(1)
        for match in re.finditer(r"[“\"《]([^”\"》]{2,32})[”\"》]", normalized)
    )
    for pattern in (
        r"([0-9A-Za-z\u3400-\u9fff_-]{2,24}?)(?:这个)?名字",
        r"([0-9A-Za-z\u3400-\u9fff_-]{2,24})为什么叫",
        r"(?:为什么)?叫([0-9A-Za-z\u3400-\u9fff_-]{2,24})",
    ):
        match = re.search(pattern, normalized)
        if match:
            candidates.append(match.group(1))
    hinted = _subquestion_subject_hint(normalized)
    if hinted:
        candidates.append(hinted)
    for candidate in candidates:
        value = re.sub(
            r"^(?:老公|老婆|宝贝|你还记得|还记得|记不记得)",
            "",
            candidate,
        )
        value = re.sub(r"(?:这个)?名字$|的名字$|这个$", "", value)
        value = value.strip("，,。！？!?；;：: 的")
        if not 2 <= len(value) <= 24:
            continue
        if value in {"这个", "那个", "名字", "我们", "为什么"}:
            continue
        if len(re.findall(r"[0-9A-Za-z\u3400-\u9fff]", value)) >= 2:
            return value
    return None


def _parse_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _temporal_retelling_query(query: str) -> Optional[str]:
    anchors = _explicit_query_anchors(query)
    if anchors:
        subject = " ".join(anchors[:3])
    else:
        subject = _normalize(query)
        for marker in _RELATIVE_SCOPE_MARKERS:
            subject = subject.replace(marker, " ")
        subject = re.sub(
            r"(?:我们|我和你|我跟你|做过|发生过|有哪些|哪些|什么|"
            r"什么时候|还记得|记不记得|呀|吗|呢)",
            " ",
            subject,
        )
        subject = re.sub(r"[，,。！？!?；;：:]", " ", subject)
        subject = _normalize(subject)
    if len(re.findall(r"[0-9A-Za-z\u3400-\u9fff]", subject)) < 2:
        return None
    return f"{subject} 后来回忆 后来复盘 后来提起"


def _filter_outside_scope_retellings(
    pass_query: str,
    response: Mapping[str, Any],
    *,
    start_at: str,
    end_at_exclusive: str,
) -> List[Dict[str, Any]]:
    topic_terms = _event_retelling_topic_terms(pass_query)
    start = _parse_utc(start_at)
    end = _parse_utc(end_at_exclusive)
    if not topic_terms or start is None or end is None:
        return []
    kept: List[Dict[str, Any]] = []
    for raw_memory in response.get("memories") or []:
        memory = dict(raw_memory)
        mentioned = _parse_utc(memory.get("created_at"))
        if mentioned is None or mentioned < end:
            continue
        compact = re.sub(
            r"\s+", "", _normalize(str(memory.get("content") or ""))
        )
        if not any(marker in compact for marker in _RETELLING_EVIDENCE_MARKERS):
            continue
        folded = compact.casefold()
        if not any(term.casefold() in folded for term in topic_terms):
            continue
        memory["temporal_evidence_role"] = (
            "later_retelling_outside_primary_scope"
        )
        kept.append(memory)
    return kept


def _event_retelling_topic_terms(pass_query: str) -> List[str]:
    subject = pass_query.split(" 后来回忆", 1)[0]
    compact = re.sub(r"\s+", "", _normalize(subject))
    for marker in (
        "我跟你",
        "我和你",
        "你跟我",
        "你和我",
        "我们俩",
        "我们两个",
        "我们",
        "咱们",
        "一起",
        "共同",
    ) + _EARLIEST_MARKERS:
        compact = compact.replace(marker, "")
    terms: List[str] = []
    for lexical in re.findall(r"[0-9A-Za-z]+|[\u3400-\u9fff]+", compact):
        if re.fullmatch(r"[\u3400-\u9fff]+", lexical):
            if len(lexical) == 2:
                terms.append(lexical)
            elif len(lexical) >= 3:
                terms.extend(
                    lexical[index : index + 3]
                    for index in range(len(lexical) - 2)
                )
        elif len(lexical) >= 2:
            terms.append(lexical.casefold())
    return list(dict.fromkeys(terms))[:16]


def _filter_event_retelling_trace(
    pass_query: str,
    response: Mapping[str, Any],
) -> Dict[str, Any]:
    """Keep the extra pass anchored to the qualifying event's literal topic."""

    filtered = copy.deepcopy(dict(response))
    topic_terms = _event_retelling_topic_terms(pass_query)
    if not topic_terms:
        filtered["memories"] = []
        return filtered
    kept = []
    for memory in response.get("memories") or []:
        content = _normalize(str(memory.get("content") or ""))
        compact = re.sub(r"\s+", "", content)
        if not any(marker in compact for marker in _RETELLING_EVIDENCE_MARKERS):
            continue
        folded = compact.casefold()
        if not any(term.casefold() in folded for term in topic_terms):
            continue
        kept.append(copy.deepcopy(dict(memory)))
    filtered["memories"] = kept
    return filtered


def _explicit_query_date(query: str) -> Optional[str]:
    """Return an explicit calendar date already present in the query."""

    normalized = _normalize(query)
    for pattern in (
        r"(?<!\d)(?P<year>20\d{2})[-/.年](?P<month>\d{1,2})[-/.月]"
        r"(?P<day>\d{1,2})(?:日|号)?(?!\d)",
    ):
        match = re.search(pattern, normalized)
        if not match:
            continue
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        try:
            # ISO's parser gives us calendar validation without introducing a
            # timezone guess.  Only strict later-day comparisons are made.
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _apply_temporal_coverage_guard(
    query: str, response: Dict[str, Any]
) -> Dict[str, Any]:
    """Fail closed when a dated event is newer than the imported archive."""

    requested_date = _explicit_query_date(query)
    if requested_date is None:
        return response
    coverage = response.get("coverage")
    latest = (
        coverage.get("latest_imported_record_at")
        if isinstance(coverage, Mapping)
        else None
    )
    latest_date = str(latest)[:10] if latest else None
    outside = bool(latest_date and requested_date > latest_date)
    response["temporal_coverage"] = {
        "schema_version": TEMPORAL_COVERAGE_SCHEMA_VERSION,
        "date_source": "explicit_query_date",
        "requested_date": requested_date,
        "latest_imported_record_at": latest,
        "status": (
            "outside_imported_coverage"
            if outside
            else "not_proven_outside_imported_coverage"
        ),
        "coverage_gap": outside,
    }
    if outside:
        # Older records sharing words such as coffee or chocolate cannot
        # support a later dated event.  Returning none is safer than inviting
        # the caller to mistake lexical similarity for evidence.
        response["memories"] = []
        response["recall_mode"] = "sqlite_temporal_coverage_guard"
        timeline = response.get("event_timeline")
        if isinstance(timeline, Mapping):
            response["event_timeline"] = suppress_event_timeline_for_coverage_gap(
                timeline
            )
        if isinstance(coverage, dict):
            coverage["coverage_gap"] = True
            coverage["coverage_status"] = "outside_imported_coverage"
    return response


def _merge_results(
    query: str,
    limit: int,
    pass_results: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    reference_instant: Optional[str] = None,
    reference_source: Optional[str] = None,
    record_filtering_applied: bool = False,
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
    has_subquestion_pass = any(
        name.startswith("subquestion_") for name, _ in ordered_passes
    )
    if has_subquestion_pass:
        ordered_passes.sort(
            key=lambda item: (
                0
                if item[0] == "original_evidence_trace"
                else 1
                if item[0].startswith("subquestion_")
                else 2
                if item[0] == "initial"
                else 3
            )
        )
    elif source_trace_sensitive:
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
    # Take one item from each evidence pass in turn.  A broad first pass may
    # contain many plausible-but-weak rows; round-robin keeps a precise trace
    # or literal-anchor pass visible inside a small public result limit.
    positions = [0] * len(ordered_passes)
    while True:
        progressed = False
        for pass_index, (pass_name, response) in enumerate(ordered_passes):
            raw_memories = list(response.get("memories") or [])
            while positions[pass_index] < len(raw_memories):
                raw_memory = raw_memories[positions[pass_index]]
                positions[pass_index] += 1
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
                progressed = True
                break
        if not progressed:
            break

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
    # This is a rebuildable response annotation over all evidence gathered by
    # the bounded internal plan, including rows that do not fit the caller's
    # ordinary memory limit. It never mutates records_v1 or asserts that two
    # similar mentions are the same real-world event.
    first["event_timeline"] = build_event_timeline(
        query,
        merged,
        reference_instant=reference_instant,
        reference_source=reference_source,
        record_filtering_applied=record_filtering_applied,
    )
    event_collection = build_event_collection(query, merged)
    if event_collection is not None:
        first["event_collection"] = event_collection
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
    reference_time_source: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one bounded memory packet from a small internal recall plan."""

    collection_intent, _ = plan_event_collection_passes(query)
    internal_ceiling = (
        MAX_COLLECTION_INTERNAL_RESULT_LIMIT
        if collection_intent is not None
        else MAX_INTERNAL_RESULT_LIMIT
    )
    internal_floor = (
        MAX_COLLECTION_INTERNAL_RESULT_LIMIT
        if collection_intent is not None
        else 8
    )
    internal_limit = min(internal_ceiling, max(limit, internal_floor))
    query_clock = resolve_query_calendar_scope(
        query,
        as_of,
        reference_time_source,
        used_for_record_filtering=True,
    )
    scope_start = query_clock.get("filter_start_at")
    scope_end = query_clock.get("filter_end_at_exclusive")
    scope_applied = bool(
        query_clock.get("used_for_record_filtering")
        and scope_start
        and scope_end
    )

    def run(pass_query: str, *, apply_scope: bool = True) -> Dict[str, Any]:
        created_at_start = scope_start if scope_applied and apply_scope else None
        created_at_end = scope_end if scope_applied and apply_scope else None
        if include_projection:
            return recall_with_projection(
                pass_query,
                agent_id=agent_id,
                limit=internal_limit,
                as_of=as_of,
                created_at_start=created_at_start,
                created_at_end_exclusive=created_at_end,
                db_path=db_path,
                read_only=read_only,
            )
        return recall_records(
            pass_query,
            agent_id=agent_id,
            limit=internal_limit,
            as_of=as_of,
            created_at_start=created_at_start,
            created_at_end_exclusive=created_at_end,
            db_path=db_path,
            read_only=read_only,
        )

    first = run(query)
    pass_results: List[tuple[str, Mapping[str, Any]]] = [("initial", first)]
    follow_up_budget = MAX_ADAPTIVE_QUERY_PASSES - (1 if scope_applied else 0)
    for pass_name, pass_query in _follow_up_passes(query, first):
        if len(pass_results) >= follow_up_budget:
            break
        result = run(pass_query)
        if pass_name == "event_retelling_trace":
            result = _filter_event_retelling_trace(pass_query, result)
        pass_results.append((pass_name, result))
    merged = _merge_results(
        query,
        limit,
        pass_results,
        reference_instant=as_of,
        reference_source=reference_time_source,
        record_filtering_applied=scope_applied,
    )
    outside_retellings: List[Dict[str, Any]] = []
    if scope_applied and len(pass_results) < MAX_ADAPTIVE_QUERY_PASSES:
        retelling_query = _temporal_retelling_query(query)
        if retelling_query:
            retelling_response = run(retelling_query, apply_scope=False)
            outside_retellings = _filter_outside_scope_retellings(
                retelling_query,
                retelling_response,
                start_at=str(scope_start),
                end_at_exclusive=str(scope_end),
            )[:3]
            merged["adaptive_recall"]["passes"].append(
                {
                    "pass": "outside_scope_retelling_trace",
                    "recall_mode": retelling_response.get("recall_mode"),
                    "result_count": len(outside_retellings),
                }
            )
            merged["adaptive_recall"]["query_passes_used"] += 1
            merged["adaptive_recall"]["budget_exhausted"] = (
                merged["adaptive_recall"]["query_passes_used"]
                >= MAX_ADAPTIVE_QUERY_PASSES
            )
    merged["temporal_scope"] = {
        **query_clock,
        "primary_evidence_policy": (
            "records inside the resolved half-open UTC interval only"
            if scope_applied
            else "no resolved record-time filter"
        ),
        "outside_scope_retellings_policy": (
            "separate labelled evidence; never primary in-range evidence"
        ),
        "outside_scope_retellings": outside_retellings,
    }
    return _apply_temporal_coverage_guard(query, merged)
