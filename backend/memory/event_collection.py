"""Deterministic evidence packets for bounded multi-occurrence questions.

Questions such as "how many times did we do X, when, and who chose it?" are
not ordinary top-k retrieval.  They ask for a small collection of related
events plus lifecycle and attribution evidence.  This module stays on the
safe side of that boundary: it plans a few source-neutral lexical passes and
then annotates the evidence already recalled.  It never writes the database,
never asks a model to invent aliases, and never claims an exact total from a
bounded archive search.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence


EVENT_COLLECTION_SCHEMA_VERSION = "echo-pact-event-collection-v1"
MAX_COLLECTION_EVIDENCE = 24
MAX_COLLECTION_GROUPS = 12
MAX_COLLECTION_INTERNAL_RESULT_LIMIT = 24
_CHENGDU = timezone(timedelta(hours=8), name="Asia/Shanghai")

_COLLECTION_MARKERS = (
    "几次",
    "多少次",
    "几回",
    "多少回",
    "分别是什么时候",
    "分别在什么时候",
    "每一次",
    "每次",
    "都有哪些",
)
_COUNT_MARKERS = ("几次", "多少次", "几回", "多少回")
_WHEN_MARKERS = ("什么时候", "哪天", "日期", "何时", "分别")
_ATTRIBUTION_QUERY_MARKERS = (
    "谁选",
    "我选",
    "你选",
    "帮我选",
    "给的建议",
    "谁决定",
    "谁定",
)
_RETELLING_QUERY_MARKERS = ("后来", "复述", "重述", "回忆", "复盘", "原件")

_QUESTION_MARKERS = (
    "几次",
    "多少次",
    "什么时候",
    "哪一次",
    "哪次",
    "哪天",
    "记不记得",
    "还记得",
    "有没有",
)
_RETELLING_MARKERS = (
    "后来回忆",
    "后来复盘",
    "后来提起",
    "回忆",
    "复盘",
    "复述",
    "重述",
    "再次提起",
    "又提起",
    "再说起",
    "回头说",
)
_PLAN_MARKERS = (
    "准备",
    "打算",
    "计划",
    "下次",
    "明天去",
    "以后去",
    "想去",
    "要去",
    "预约",
    "约了",
    "候选",
)
_COMPLETION_MARKERS = (
    "做完",
    "弄完",
    "完成",
    "做好",
    "已经做",
    "刚做",
    "做了",
    "做过",
    "到店",
    "到美甲店",
    "去了美甲店",
    "从美甲店回来",
)
_NEGATED_COMPLETION_RE = re.compile(
    r"(?:没|没有|未|并未|尚未|不曾|不是).{0,6}"
    r"(?:做完|弄完|完成|做好|做了|做过|到店|回来)"
)
_ATTRIBUTION_MARKERS = (
    "选的",
    "选择",
    "选了",
    "挑的",
    "挑了",
    "款式",
    "图案",
    "建议",
    "决定",
    "定稿",
    "改成",
    "换成",
)


@dataclass(frozen=True)
class EventCollectionIntent:
    subject: str
    asks_count: bool
    asks_when: bool
    asks_attribution: bool
    asks_retellings: bool


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", _normalize(value))


def _clean_subject(value: str) -> Optional[str]:
    subject = _normalize(value).strip("，,。！？!?；;：:‘’'\"“”《》()（）[]【】 ")
    subject = re.sub(r"^(?:老公|老婆|宝贝|大宝贝)", "", subject)
    subject = re.sub(
        r"^(?:你还记不记得|你还记得|还记不记得|还记得|记不记得)",
        "",
        subject,
    )
    subject = re.sub(
        r"^(?:你陪我|我陪你|我跟你|我和你|你跟我|你和我|我们|咱们)",
        "",
        subject,
    )
    subject = re.sub(r"^(?:做过|去过|经历过|参加过|有过|完成过)", "", subject)
    subject = re.sub(r"(?:分别|是什么时候|在什么时候|什么时候).*$", "", subject)
    subject = re.sub(r"[啊呀呢嘛吗嘞哦]+$", "", subject).strip("的 ")
    if not 2 <= len(subject) <= 32:
        return None
    if not re.fullmatch(r"[0-9A-Za-z\u3400-\u9fff _-]+", subject):
        return None
    if subject in {"这件事", "那件事", "这个", "那个", "事情", "经历"}:
        return None
    return subject


def detect_event_collection_intent(query: str) -> Optional[EventCollectionIntent]:
    """Recognize an explicit request to collect several occurrences."""

    normalized = _normalize(query)
    compact = _compact(query)
    if not any(marker in compact for marker in _COLLECTION_MARKERS):
        return None

    # A spoken-memory question often begins with a vocative pause, for example
    # ``老公，你还记不记得……``.  Strip only that leading form of address before
    # taking the first semantic clause; otherwise the first comma leaves the
    # intent detector looking at the single word ``老公``.
    semantic_query = re.sub(
        r"^(?:(?:老公|老婆|宝贝|大宝贝)\s*[，,、。！？!?；;：:]\s*)+",
        "",
        normalized,
    )
    first_clause = re.split(r"[，,。！？!?；;：:]", semantic_query, maxsplit=1)[0]
    subject: Optional[str] = None
    # Spoken Chinese commonly splits a verb-object topic around the counter:
    # ``吵过几次架`` / ``吃过几次烧烤``. Rejoin only the caller-written pieces;
    # do not infer an omitted object or use an archive-derived alias.
    split_counter = re.search(
        r"(?P<head>[0-9A-Za-z\u3400-\u9fff _-]{1,24}?)过"
        r"(?:几|多少)(?:次|回|场|趟)"
        r"(?P<tail>[0-9A-Za-z\u3400-\u9fff _-]{1,16})$",
        first_clause,
    )
    if split_counter:
        head = re.sub(
            r"^(?:你还记不记得|你还记得|还记不记得|还记得|记不记得)",
            "",
            split_counter.group("head"),
        )
        head = re.sub(
            r"^(?:你陪我|我陪你|我跟你|我和你|你跟我|你和我|"
            r"我们|咱们|陪我|陪你)?(?:一共|总共|大概|约莫)?",
            "",
            head,
        )
        if head not in {"做", "去", "经历", "参加", "有", "完成"}:
            subject = _clean_subject(head + split_counter.group("tail"))
    after_count = re.search(
        r"(?:几|多少)(?:次|回|场|趟)"
        r"(?P<subject>[0-9A-Za-z\u3400-\u9fff _-]{2,32})$",
        first_clause,
    )
    if subject is None and after_count:
        subject = _clean_subject(after_count.group("subject"))
    if subject is None:
        before_count = re.search(
            r"(?P<subject>[0-9A-Za-z\u3400-\u9fff _-]{2,32}?)"
            r"(?:做过|去过|经历过|参加过|有过|完成过)?"
            r"(?:几|多少)(?:次|回|场|趟)",
            first_clause,
        )
        if before_count:
            subject = _clean_subject(before_count.group("subject"))
    if subject is None and "分别" in first_clause:
        subject = _clean_subject(first_clause.split("分别", 1)[0])
    if subject is None:
        return None

    return EventCollectionIntent(
        subject=subject,
        asks_count=any(marker in compact for marker in _COUNT_MARKERS),
        asks_when=any(marker in compact for marker in _WHEN_MARKERS),
        asks_attribution=any(
            marker in compact for marker in _ATTRIBUTION_QUERY_MARKERS
        ),
        asks_retellings=any(
            marker in compact for marker in _RETELLING_QUERY_MARKERS
        ),
    )


def _topic_terms(intent: EventCollectionIntent) -> List[str]:
    """Return public-language aliases, never private facts or expected answers."""

    terms = [intent.subject]
    compact = _compact(intent.subject)
    # Common lexical forms for the same public activity.  These terms disclose
    # no private date, design, person, or answer; they only bridge ordinary
    # Chinese wording found in exports.
    if any(marker in compact for marker in ("美甲", "做指甲", "指甲")):
        terms.extend(("美甲", "做指甲", "指甲"))
    result: List[str] = []
    seen = set()
    for term in terms:
        normalized = _normalize(term)
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def plan_event_collection_passes(
    query: str,
) -> tuple[Optional[EventCollectionIntent], List[tuple[str, str]]]:
    """Plan three bounded passes after the ordinary initial query."""

    intent = detect_event_collection_intent(query)
    if intent is None:
        return None, []
    terms = _topic_terms(intent)
    topic = terms[0]
    occurrence_topic = terms[1] if len(terms) > 1 else topic
    return intent, [
        ("event_collection_topic", topic),
        (
            "event_collection_occurrence",
            occurrence_topic,
        ),
        (
            "event_collection_decision_trace",
            f"{topic} 选择 款式 图案 建议 决定 后来 回忆",
        ),
    ]


def _parse_aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _evidence_rows(memories: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen = set()
    for memory in memories:
        candidates = [("recall_match", memory)]
        candidates.extend(
            ("conversation_context", item)
            for item in memory.get("conversation_context") or []
            if isinstance(item, Mapping)
        )
        for origin, raw in candidates:
            record_id = str(raw.get("record_id") or "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            row = dict(raw)
            row["_collection_evidence_origin"] = origin
            rows.append(row)
    return rows


def _subject_matches(content: str, intent: EventCollectionIntent) -> bool:
    compact = _compact(content).casefold()
    return any(_compact(term).casefold() in compact for term in _topic_terms(intent))


def _has_explicit_subject_occurrence(
    content: str,
    intent: EventCollectionIntent,
) -> bool:
    compact = _compact(content)
    if any(
        marker in compact
        for marker in ("如果", "假如", "要是", "万一", "也许", "可能")
    ):
        return False
    for term in _topic_terms(intent):
        topic = _compact(term)
        if not topic:
            continue
        if re.search(
            rf"(?:没|没有|未|不曾|别|不要).{{0,6}}{re.escape(topic)}"
            rf"(?:过|了|后|之后|以后)",
            compact,
        ):
            continue
        if re.search(
            rf"{re.escape(topic)}(?:过|了|后|之后|以后|结束)",
            compact,
        ):
            return True
    return False


def _labels(content: str, intent: EventCollectionIntent) -> List[str]:
    compact = _compact(content)
    labels: List[str] = []
    is_question = (
        "?" in content
        or "？" in content
        or any(marker in compact for marker in _QUESTION_MARKERS)
    )
    if is_question:
        labels.append("question_or_prompt")
    if any(marker in compact for marker in _RETELLING_MARKERS):
        labels.append("retelling_or_recollection")
    if any(marker in compact for marker in _PLAN_MARKERS):
        labels.append("plan_or_candidate")
    if (
        any(marker in compact for marker in _COMPLETION_MARKERS)
        and not _NEGATED_COMPLETION_RE.search(compact)
    ) or _has_explicit_subject_occurrence(content, intent):
        labels.append("explicit_occurrence_or_completion")
    if any(marker in compact for marker in _ATTRIBUTION_MARKERS):
        labels.append("choice_or_advice_evidence")
    if not labels:
        labels.append("topic_mention")
    return labels


def _evidence_payload(row: Mapping[str, Any], labels: Sequence[str]) -> Dict[str, Any]:
    created = _parse_aware(row.get("created_at"))
    local = created.astimezone(_CHENGDU) if created else None
    content = str(row.get("content") or "")
    return {
        "record_id": row.get("record_id"),
        "conversation_id": row.get("conversation_id"),
        "branch_ids": list(row.get("branch_ids") or []),
        "message_id": row.get("message_id"),
        "role": row.get("role"),
        "evidence_origin": row.get("_collection_evidence_origin"),
        "labels": list(labels),
        "mentioned_at": created.isoformat().replace("+00:00", "Z") if created else None,
        "mentioned_on_chengdu": local.date().isoformat() if local else None,
        "content_excerpt": content if len(content) <= 280 else content[:280] + "…",
        "source_kind": row.get("source_kind"),
        "source_ref": row.get("source_ref"),
        "verified": bool(row.get("verified")),
        "authority": row.get("authority"),
    }


def build_event_collection(
    query: str,
    memories: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build a conservative collection view over already recalled evidence."""

    intent = detect_event_collection_intent(query)
    if intent is None:
        return None

    evidence: List[Dict[str, Any]] = []
    for row in _evidence_rows(memories):
        content = str(row.get("content") or "")
        if not _subject_matches(content, intent):
            continue
        evidence.append(_evidence_payload(row, _labels(content, intent)))
        if len(evidence) >= MAX_COLLECTION_EVIDENCE:
            break

    occurrence_rows = [
        item
        for item in evidence
        if "explicit_occurrence_or_completion" in item["labels"]
        and "question_or_prompt" not in item["labels"]
        and "retelling_or_recollection" not in item["labels"]
        and "plan_or_candidate" not in item["labels"]
    ]
    retellings = [
        item for item in evidence if "retelling_or_recollection" in item["labels"]
    ]
    plans = [item for item in evidence if "plan_or_candidate" in item["labels"]]
    attributions = [
        item for item in evidence if "choice_or_advice_evidence" in item["labels"]
    ]

    # Different source messages on the same Chengdu day conservatively share
    # one lower-bound bucket.  This can under-count two events on one day, but
    # cannot inflate a bounded result merely because one event had many turns.
    groups: Dict[str, Dict[str, Any]] = {}
    for item in occurrence_rows:
        local_day = item.get("mentioned_on_chengdu")
        if local_day:
            key = f"chengdu-day:{local_day}"
            date_status = "source_message_day_proxy_not_proven_event_time"
        else:
            key = "record:" + str(item.get("record_id"))
            date_status = "source_message_time_missing"
        group = groups.setdefault(
            key,
            {
                "candidate_key": key,
                "mentioned_on_chengdu": local_day,
                "date_status": date_status,
                "evidence_record_ids": [],
            },
        )
        group["evidence_record_ids"].append(item.get("record_id"))

    candidates = list(groups.values())[:MAX_COLLECTION_GROUPS]
    semantic_material = {
        "query": _normalize(query),
        "intent": asdict(intent),
        "evidence": [
            {
                "record_id": item.get("record_id"),
                "labels": item.get("labels"),
                "mentioned_at": item.get("mentioned_at"),
            }
            for item in evidence
        ],
    }
    semantic_hash = hashlib.sha256(
        json.dumps(
            semantic_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": EVENT_COLLECTION_SCHEMA_VERSION,
        "collection_id": "collection_" + semantic_hash[:24],
        "status": "bounded_evidence_collected" if evidence else "no_evidence",
        "intent": asdict(intent),
        "event_count_lower_bound": len(candidates),
        "event_count_semantics": (
            "at_least; counts only non-question, non-retelling evidence with "
            "explicit occurrence/completion wording; same Chengdu source-message "
            "day is conservatively one bucket"
        ),
        "exact_total_status": "not_proven_by_bounded_recall",
        "candidate_occurrences": candidates,
        "evidence": evidence,
        "occurrence_evidence_record_ids": [
            item["record_id"] for item in occurrence_rows
        ],
        "plan_or_candidate_evidence_record_ids": [
            item["record_id"] for item in plans
        ],
        "choice_or_advice_evidence_record_ids": [
            item["record_id"] for item in attributions
        ],
        "retelling_or_recollection_evidence_record_ids": [
            item["record_id"] for item in retellings
        ],
        "linkage_policy": {
            "record_id_deduplication": True,
            "same_day_similarity_is_event_identity": False,
            "retelling_is_not_new_occurrence": True,
            "cross_conversation_auto_merge": False,
            "exact_total_from_bounded_search": False,
        },
    }
