"""Conservative evidence packets for named-item collection questions.

Questions such as "how many bracelets did we choose, and what are their
names?" are inventory questions, not repeated-event questions.  This module
recognises that public-language shape, plans a few bounded lexical passes, and
classifies only the evidence already returned by the read-only recall path.

The implementation is deliberately source-neutral.  It contains no private
item names, calls no model or network service, never writes the archive, and
never presents a bounded search as a proven exhaustive total.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


NAMED_COLLECTION_SCHEMA_VERSION = "echo-pact-named-collection-v1"
MAX_NAMED_COLLECTION_EVIDENCE = 8
MAX_NAMED_COLLECTION_ITEMS = 20
MAX_NAMED_COLLECTION_INTERNAL_RESULT_LIMIT = 32
MAX_ITEM_EVIDENCE_IDS = 4

_COUNT_UNITS = (
    "个",
    "件",
    "串",
    "条",
    "款",
    "本",
    "瓶",
    "只",
    "枚",
    "双",
    "辆",
    "台",
    "份",
    "种",
    "套",
)
_COUNT_MARKERS = tuple(
    prefix + unit for prefix in ("几", "多少") for unit in _COUNT_UNITS
)
_NAME_QUERY_MARKERS = (
    "叫什么",
    "名字",
    "名称",
    "分别叫",
    "都叫",
    "有哪些",
    "哪几个",
    "哪几件",
    "哪几串",
)
_PLURAL_REFERENCE_MARKERS = ("它们", "这些", "那些", "分别", "都有哪些")
_JOINT_QUERY_MARKERS = (
    "一起选",
    "共同选",
    "一起挑",
    "共同挑",
    "一起配",
    "共同配",
    "我们选",
    "我们挑",
    "帮我选",
    "帮你选",
)

_NAMING_MARKERS = (
    "正式命名",
    "命名为",
    "取名为",
    "起名为",
    "名字定为",
    "名字定成",
    "名字是",
    "名称是",
    "就叫",
    "叫作",
    "叫做",
)
_CONFIRMED_MARKERS = _NAMING_MARKERS + (
    "正式定名",
    "最终定名",
    "共同选定",
    "一起选定",
    "已经选定",
    "选好了",
    "挑好了",
    "下单",
    "收到",
    "完成",
    "搞定",
)
_CANDIDATE_MARKERS = (
    "候选",
    "备选",
    "可以叫",
    "想叫",
    "暂定",
    "要不要叫",
    "考虑叫",
    "还没下单",
    "没有下单",
    "未下单",
    "没有最终定名",
    "未最终定名",
    "尚未定名",
)
_RETELLING_MARKERS = (
    "后来",
    "回忆",
    "复盘",
    "复述",
    "重述",
    "又提起",
    "再次提起",
    "再说起",
)
_SOLO_MARKERS = (
    "我一个人选",
    "你一个人选",
    "我自己选",
    "你自己选",
    "独自选",
    "不是我们一起",
    "不属于我们一起",
    "并非我们一起",
)
_JOINT_EVIDENCE_RE = re.compile(
    r"(?:我们|咱们|我(?:跟|和)你|你(?:跟|和)我|共同|一起).{0,12}"
    r"(?:选|挑|配|定)"
    r"|(?:选|挑|配|定).{0,12}(?:我们|咱们|共同|一起)"
)
_NAMING_EVIDENCE_RE = re.compile(
    r"(?:名字|名称).{0,8}(?:定|叫|命名)"
    r"|(?:正式|最终).{0,6}(?:定名|命名|取名)"
)
_QUESTIONISH_NAME_MARKERS = (
    "什么",
    "怎么",
    "为什么",
    "是不是",
    "要不要",
    "记不记得",
    "还记得",
)
_NON_NAME_FRAGMENT_MARKERS = (
    "有没有",
    "很可能",
    "大概率",
    "再看看",
    "故事与配图",
    "选珠顺序",
    "顺序与特点",
    "猫儿子动态",
    "饰品名称",
    "记录说明",
    "列表标题",
)
_LOCAL_CONTEXT_RADIUS = 240

# Public category aliases only.  They bridge ordinary wording without adding
# a private answer.  More sources can add adapters without changing this core.
_PUBLIC_SUBJECT_ALIASES = {
    "手串": ("手串", "珠串", "串珠"),
    "珠串": ("珠串", "手串", "串珠"),
}
_RELATED_BUT_DISTINCT_TYPES = {
    "手串": ("手镯", "项链", "脚链", "戒指", "耳环", "耳饰"),
    "珠串": ("手镯", "项链", "脚链", "戒指", "耳环", "耳饰"),
}


@dataclass(frozen=True)
class NamedCollectionIntent:
    subject: str
    unit: Optional[str]
    asks_count: bool
    asks_names: bool
    relation_scope: str


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", _normalize(value))


def _unique_text(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for raw in values:
        value = _normalize(raw)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _clean_subject(value: str) -> Optional[str]:
    subject = _normalize(value).strip("，,。！？!?；;：:‘’'\"“”《》()（）[]【】 ")
    subject = re.sub(r"^(?:老公|老婆|宝贝|大宝贝)", "", subject)
    subject = re.sub(
        r"^(?:你还记不记得|你还记得|还记不记得|还记得|记不记得)",
        "",
        subject,
    )
    subject = re.sub(
        r"^(?:我们|咱们|我(?:跟|和)你|你(?:跟|和)我)"
        r"(?:一起|共同)?(?:选过|选的|选|挑过|挑的|挑|配过|配的|配)?",
        "",
        subject,
    )
    subject = re.split(
        r"(?:你还记得|还记得|记不记得|叫什么|名字|名称|分别|有哪些)",
        subject,
        maxsplit=1,
    )[0]
    subject = re.sub(r"[啊呀呢嘛吗嘞哦]+$", "", subject).strip("的 ")
    if not 1 <= len(subject) <= 24:
        return None
    if not re.fullmatch(r"[0-9A-Za-z\u3400-\u9fff _-]+", subject):
        return None
    if subject in {"这个", "那个", "东西", "物品", "名字", "名称", "事情"}:
        return None
    return subject


def detect_named_collection_intent(query: str) -> Optional[NamedCollectionIntent]:
    """Recognise explicit plural inventory/name questions.

    Units such as 次/回 deliberately stay with event_collection.  A singular
    question like "do you like this bracelet?" must not activate this packet.
    """

    normalized = _normalize(query)
    compact = _compact(query)
    asks_count = any(marker in compact for marker in _COUNT_MARKERS)
    asks_names = any(marker in compact for marker in _NAME_QUERY_MARKERS)
    has_plural_reference = any(
        marker in compact for marker in _PLURAL_REFERENCE_MARKERS
    )
    if not asks_count and not (asks_names and has_plural_reference):
        return None

    semantic_query = re.sub(
        r"^(?:(?:老公|老婆|宝贝|大宝贝)\s*[，,、。！？!?；;：:]\s*)+",
        "",
        normalized,
    )
    first_clause = re.split(r"[，,。！？!?；;：:]", semantic_query, maxsplit=1)[0]
    subject: Optional[str] = None
    unit: Optional[str] = None
    unit_group = "|".join(map(re.escape, _COUNT_UNITS))
    after_count = re.search(
        rf"(?:几|多少)(?P<unit>{unit_group})"
        r"(?P<subject>[0-9A-Za-z\u3400-\u9fff _-]{1,24})",
        first_clause,
    )
    if after_count:
        unit = after_count.group("unit")
        subject = _clean_subject(after_count.group("subject"))
    if subject is None:
        before_count = re.search(
            r"(?P<subject>[0-9A-Za-z\u3400-\u9fff _-]{1,24}?)"
            r"(?:有|选过|挑过|买过|收过|做过)?"
            rf"(?:几|多少)(?P<unit>{unit_group})",
            first_clause,
        )
        if before_count:
            unit = before_count.group("unit")
            subject = _clean_subject(before_count.group("subject"))
    if subject is None and asks_names:
        subject_match = re.search(
            r"(?P<subject>[0-9A-Za-z\u3400-\u9fff _-]{1,24})"
            r"(?:都有哪些|有哪些|分别叫什么|叫什么名字)",
            first_clause,
        )
        if subject_match:
            subject = _clean_subject(subject_match.group("subject"))
    if subject is None:
        return None

    relation_scope = (
        "joint_selection"
        if any(marker in compact for marker in _JOINT_QUERY_MARKERS)
        or bool(
            re.search(
                r"(?:我们|咱们|我(?:跟|和)你|你(?:跟|和)我).{0,8}"
                r"(?:选|挑|配)",
                compact,
            )
        )
        else "unspecified"
    )
    return NamedCollectionIntent(
        subject=subject,
        unit=unit,
        asks_count=asks_count,
        asks_names=asks_names,
        relation_scope=relation_scope,
    )


def _topic_terms(intent: NamedCollectionIntent) -> List[str]:
    aliases = _PUBLIC_SUBJECT_ALIASES.get(intent.subject, (intent.subject,))
    return _unique_text((intent.subject, *aliases))


def plan_named_collection_passes(
    query: str,
) -> tuple[Optional[NamedCollectionIntent], List[tuple[str, str]]]:
    """Plan three deterministic, bounded evidence searches."""

    intent = detect_named_collection_intent(query)
    if intent is None:
        return None, []
    terms = _topic_terms(intent)
    topic = terms[0]
    alias_topic = terms[1] if len(terms) > 1 else topic
    return intent, [
        ("named_collection_subject", topic),
        (
            "named_collection_names",
            f"{alias_topic} 名字",
        ),
        (
            "named_collection_relation",
            f"{topic} 一起选 命名",
        ),
    ]


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
            row["_named_collection_evidence_origin"] = origin
            rows.append(row)
    return rows


def _subject_matches(content: str, intent: NamedCollectionIntent) -> bool:
    compact = _compact(content)
    folded = compact.casefold()
    aliases = _topic_terms(intent)
    if not any(_compact(alias).casefold() in folded for alias in aliases):
        return False

    # Explicit source-language exclusions are stronger than a stray mention of
    # the requested category ("this necklace must not count as a bracelet").
    distinct_types = _RELATED_BUT_DISTINCT_TYPES.get(intent.subject, ())
    if any(distinct in compact for distinct in distinct_types) and re.search(
        rf"(?:不是|不算|不要算进|不属于).{{0,8}}{re.escape(intent.subject)}"
        rf"|{re.escape(intent.subject)}.{{0,8}}(?:不是|不算|不要算进|不属于)",
        compact,
    ):
        return False
    return True


@dataclass(frozen=True)
class _NamedMention:
    display_name: str
    context: str
    extraction_kind: str


def _clean_name(value: str) -> str:
    return _normalize(value).strip(
        "，,。！？!?；;：:‘’'\"“”《》()（）[]【】 *•·—-_"
    )


def _valid_name(value: str) -> bool:
    name = _clean_name(value)
    if not 2 <= len(name) <= 16:
        return False
    if name in {
        "这个",
        "那个",
        "手串",
        "珠串",
        "名字",
        "名称",
        "为",
        "成",
        "作",
        "做",
    }:
        return False
    if any(marker in name for marker in _QUESTIONISH_NAME_MARKERS):
        return False
    if any(marker in name for marker in _NON_NAME_FRAGMENT_MARKERS):
        return False
    if name.endswith(("取的", "选的", "挑的", "配的", "叫的", "命名的")):
        return False
    if "?" in name or "？" in name:
        return False
    return len(re.findall(r"[0-9A-Za-z\u3400-\u9fff]", name)) >= 1


def _nearby_context(content: str, start: int, end: int) -> str:
    left = max(0, start - _LOCAL_CONTEXT_RADIUS)
    right = min(len(content), end + _LOCAL_CONTEXT_RADIUS)
    # Keep a small amount of adjacent-line context: exports frequently place a
    # shared-selection statement in a heading and the item name in the next
    # Markdown row. The fixed radius still prevents a long notebook record
    # from lending unrelated evidence from a distant section.
    return content[left:right]


def _nearest_subject_distance(
    context: str,
    name_start: int,
    intent: NamedCollectionIntent,
) -> Optional[int]:
    distances = [
        abs(match.start() - name_start)
        for alias in _topic_terms(intent)
        for match in re.finditer(re.escape(alias), context, flags=re.IGNORECASE)
    ]
    return min(distances) if distances else None


def _nearest_naming_distance(context: str, name_start: int) -> Optional[int]:
    positions = [
        match.start()
        for marker in _NAMING_MARKERS
        for match in re.finditer(re.escape(marker), context)
    ]
    positions.extend(match.start() for match in _NAMING_EVIDENCE_RE.finditer(context))
    return min((abs(position - name_start) for position in positions), default=None)


def _extract_named_mentions(
    content: str,
    intent: NamedCollectionIntent,
) -> List[_NamedMention]:
    """Extract names only when item and naming evidence are locally linked."""

    mentions: List[_NamedMention] = []
    quoted_pattern = re.compile(
        r"《(?P<book>[^》\r\n]{1,64})》"
        r"|[“「『](?P<cjk>[^”」』\r\n]{1,64})[”」』]"
        r"|\"(?P<ascii>[^\"\r\n]{1,64})\""
    )
    for match in quoted_pattern.finditer(content):
        name = match.group("book") or match.group("cjk") or match.group("ascii")
        context = _nearby_context(content, match.start(), match.end())
        if not _valid_name(name):
            continue
        local_name_start = context.find(name)
        if local_name_start < 0:
            local_name_start = len(context) // 2
        subject_distance = _nearest_subject_distance(
            context, local_name_start, intent
        )
        naming_distance = _nearest_naming_distance(context, local_name_start)
        if not _subject_matches(context, intent) or not (
            (subject_distance is not None and subject_distance <= 16)
            or (naming_distance is not None and naming_distance <= 64)
        ):
            continue
        mentions.append(_NamedMention(_clean_name(name), context, "quoted_near_subject"))

    # Direct unquoted naming remains supported, but only beside the requested
    # item subject. Long transcript rows cannot donate an unrelated phrase.
    for marker in sorted(_NAMING_MARKERS, key=len, reverse=True):
        pattern = re.compile(
            re.escape(marker)
            + r"\s*[：:]?\s*([0-9A-Za-z\u3400-\u9fff _·—-]{1,48})"
        )
        for match in pattern.finditer(content):
            value = re.split(
                r"[，,。！？!?；;：:\r\n]", match.group(1), maxsplit=1
            )[0].strip()
            context = _nearby_context(content, match.start(), match.end())
            local_name_start = context.find(value)
            subject_distance = _nearest_subject_distance(
                context,
                local_name_start if local_name_start >= 0 else len(context) // 2,
                intent,
            )
            if (
                not _valid_name(value)
                or not _subject_matches(context, intent)
                or subject_distance is None
                or subject_distance > 48
            ):
                continue
            mentions.append(_NamedMention(_clean_name(value), context, "direct_naming"))

    # Markdown tables and concise inventories often encode the item name as
    # ``subject: name`` or ``subject | name`` without quotes or a naming verb.
    aliases = "|".join(
        sorted((re.escape(term) for term in _topic_terms(intent)), key=len, reverse=True)
    )
    adjacent_pattern = re.compile(
        rf"(?:{aliases})\s*(?:名字|名称)?\s*[：:|｜·—-]\s*"
        r"(?:\*\*)?([0-9A-Za-z\u3400-\u9fff_·—-]{1,48}?)(?:\*\*)?"
        r"(?=$|[，,。！？!?；;：:|｜()（）\r\n])"
    )
    for match in adjacent_pattern.finditer(content):
        value = match.group(1).strip(" *")
        context = _nearby_context(content, match.start(), match.end())
        if _valid_name(value) and _subject_matches(context, intent):
            mentions.append(
                _NamedMention(_clean_name(value), context, "subject_delimited_name")
            )

    unique: List[_NamedMention] = []
    seen = set()
    for mention in mentions:
        key = (
            mention.display_name.casefold(),
            _normalize(mention.context),
            mention.extraction_kind,
        )
        if key not in seen:
            seen.add(key)
            unique.append(mention)
    return unique


def _labels(content: str, intent: NamedCollectionIntent) -> List[str]:
    compact = _compact(content)
    labels: List[str] = []
    if any(marker in compact for marker in _CANDIDATE_MARKERS):
        labels.append("candidate_or_unconfirmed")
    if any(marker in compact for marker in _SOLO_MARKERS):
        labels.append("outside_requested_relation")
    elif _JOINT_EVIDENCE_RE.search(compact):
        labels.append("joint_selection_evidence")
    if any(marker in compact for marker in _CONFIRMED_MARKERS) or (
        _NAMING_EVIDENCE_RE.search(compact)
    ):
        labels.append("confirmation_or_naming_evidence")
    if any(marker in compact for marker in _RETELLING_MARKERS):
        labels.append("retelling_or_recollection")
    if _extract_named_mentions(content, intent):
        labels.append("explicit_name")
    if not labels:
        labels.append("topic_mention")
    if (
        intent.relation_scope == "joint_selection"
        and "joint_selection_evidence" not in labels
        and "candidate_or_unconfirmed" not in labels
    ):
        labels.append("relation_not_proven")
    return labels


def _mention_labels(
    mention: _NamedMention,
    intent: NamedCollectionIntent,
) -> List[str]:
    """Keep relationship evidence locally tied to this exact name mention."""

    labels = _labels(mention.context, intent)
    if "joint_selection_evidence" not in labels:
        return labels
    name_start = mention.context.find(mention.display_name)
    joint_distances = [
        abs(match.start() - name_start)
        for match in _JOINT_EVIDENCE_RE.finditer(mention.context)
    ]
    if name_start < 0 or not joint_distances or min(joint_distances) > 32:
        labels = [
            label for label in labels if label != "joint_selection_evidence"
        ]
        if "relation_not_proven" not in labels:
            labels.append("relation_not_proven")
    return labels


def _evidence_payload(row: Mapping[str, Any], labels: Sequence[str]) -> Dict[str, Any]:
    content = str(row.get("content") or "")
    return {
        "record_id": row.get("record_id"),
        "conversation_id": row.get("conversation_id"),
        "branch_ids": list(row.get("branch_ids") or []),
        "message_id": row.get("message_id"),
        "role": row.get("role"),
        "evidence_origin": row.get("_named_collection_evidence_origin"),
        "labels": list(labels),
        "mentioned_at": row.get("created_at"),
        "content_excerpt": content if len(content) <= 280 else content[:280] + "…",
        "source_kind": row.get("source_kind"),
        "source_ref": row.get("source_ref"),
        "verified": bool(row.get("verified")),
        "authority": row.get("authority"),
    }


def _update_item_fact(
    items: Dict[str, Dict[str, Any]],
    name: str,
    evidence: Mapping[str, Any],
    labels: Sequence[str],
) -> None:
    key = _normalize(name).casefold()
    item = items.setdefault(
        key,
        {
            "display_name": _normalize(name),
            "evidence_record_ids": [],
            "first_mentioned_at": evidence.get("mentioned_at"),
            "last_mentioned_at": evidence.get("mentioned_at"),
            "has_confirmation": False,
            "has_joint_relation": False,
            "has_candidate": False,
            "has_explicit_name": False,
        },
    )
    record_id = evidence.get("record_id")
    if (
        record_id
        and record_id not in item["evidence_record_ids"]
        and len(item["evidence_record_ids"]) < MAX_ITEM_EVIDENCE_IDS
    ):
        item["evidence_record_ids"].append(record_id)
    mentioned = evidence.get("mentioned_at")
    if mentioned:
        first = item.get("first_mentioned_at")
        last = item.get("last_mentioned_at")
        item["first_mentioned_at"] = min(first, mentioned) if first else mentioned
        item["last_mentioned_at"] = max(last, mentioned) if last else mentioned
    if "candidate_or_unconfirmed" in labels:
        item["has_candidate"] = True
    elif "confirmation_or_naming_evidence" in labels:
        item["has_confirmation"] = True
    if "explicit_name" in labels:
        item["has_explicit_name"] = True
    if "joint_selection_evidence" in labels:
        item["has_joint_relation"] = True


def build_named_collection(
    query: str,
    memories: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build a lower-bound named inventory over already recalled evidence."""

    intent = detect_named_collection_intent(query)
    if intent is None:
        return None

    evidence: List[Dict[str, Any]] = []
    item_facts: Dict[str, Dict[str, Any]] = {}
    excluded_record_ids: List[str] = []

    for row in _evidence_rows(memories):
        content = str(row.get("content") or "")
        if not _subject_matches(content, intent):
            continue
        labels = _labels(content, intent)
        payload = _evidence_payload(row, labels)
        mentions = _extract_named_mentions(content, intent)

        if not mentions:
            if "outside_requested_relation" in labels:
                excluded_record_ids.append(str(row.get("record_id") or ""))
                continue
            if len(evidence) < MAX_NAMED_COLLECTION_EVIDENCE:
                evidence.append(payload)
            continue

        classified_any = False
        for mention in mentions:
            mention_labels = _mention_labels(mention, intent)
            if "outside_requested_relation" in mention_labels:
                excluded_record_ids.append(str(row.get("record_id") or ""))
                continue
            _update_item_fact(
                item_facts,
                mention.display_name,
                payload,
                mention_labels,
            )
            classified_any = True
        if not classified_any:
            # A named mention without confirmation remains evidence, not an
            # item the caller should treat as selected or final.
            payload["labels"].append("named_but_status_not_proven")

        if len(evidence) < MAX_NAMED_COLLECTION_EVIDENCE:
            evidence.append(payload)

    confirmed: Dict[str, Dict[str, Any]] = {}
    candidates: Dict[str, Dict[str, Any]] = {}
    unresolved: Dict[str, Dict[str, Any]] = {}
    for key, fact in item_facts.items():
        public_item = {
            field: value
            for field, value in fact.items()
            if not field.startswith("has_")
        }
        relation_ok = (
            intent.relation_scope != "joint_selection"
            or fact["has_joint_relation"]
        )
        if fact["has_confirmation"] and relation_ok:
            public_item["relationship_status"] = "requested_relation_supported"
            confirmed[key] = public_item
        elif fact["has_candidate"]:
            public_item["relationship_status"] = "candidate_not_final"
            candidates[key] = public_item
        elif fact["has_explicit_name"]:
            public_item["relationship_status"] = (
                "requested_relation_not_proven_in_bounded_evidence"
                if not relation_ok
                else "name_confirmation_not_proven_in_bounded_evidence"
            )
            unresolved[key] = public_item

    confirmed_items = sorted(
        confirmed.values(),
        key=lambda item: (
            str(item.get("first_mentioned_at") or ""),
            item["display_name"],
        ),
    )[:MAX_NAMED_COLLECTION_ITEMS]
    candidate_items = sorted(
        candidates.values(),
        key=lambda item: (
            str(item.get("first_mentioned_at") or ""),
            item["display_name"],
        ),
    )[:MAX_NAMED_COLLECTION_ITEMS]
    unresolved_items = sorted(
        unresolved.values(),
        key=lambda item: (
            -len(item.get("evidence_record_ids") or []),
            str(item.get("first_mentioned_at") or ""),
            item["display_name"],
        ),
    )[:MAX_NAMED_COLLECTION_ITEMS]

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
        "schema_version": NAMED_COLLECTION_SCHEMA_VERSION,
        "collection_id": "named_collection_" + semantic_hash[:24],
        "status": "bounded_evidence_collected" if evidence else "no_evidence",
        "intent": asdict(intent),
        "named_item_count_lower_bound": len(confirmed_items),
        "count_semantics": (
            "at_least; counts distinct explicitly named and confirmed items "
            "whose evidence satisfies the requested relationship scope"
        ),
        "exact_total_status": "not_proven_by_bounded_recall",
        "confirmed_items": confirmed_items,
        "candidate_items": candidate_items,
        "unresolved_items": unresolved_items,
        "evidence": evidence,
        "excluded_relation_evidence_record_ids": [
            value for value in excluded_record_ids if value
        ],
        "linkage_policy": {
            "normalized_name_deduplication": True,
            "retelling_is_not_new_item": True,
            "candidate_is_not_confirmed": True,
            "related_item_type_is_not_requested_type": True,
            "cross_conversation_auto_merge_by_unnamed_similarity": False,
            "exact_total_from_bounded_search": False,
        },
    }
