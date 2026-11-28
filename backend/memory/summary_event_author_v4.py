"""Select host-extracted date occurrences; the model never authors source quotes."""
from __future__ import annotations

import copy
import json
import re

from jsonschema import Draft202012Validator

from .caretaker_summary import SummaryError, _object
from .caretaker_summary_v2 import _explicit_dates
from .summary_faithfulness import canonical_date
from . import summary_event_author_v2 as previous
from . import summary_reading_card as reading

RECIPE = "echo-summary-event-author-v4-date-index"
DATES = re.compile(r"(?<!\d)(?:\d{4}年\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2})(?!\d)")
SYSTEM_PROMPT = previous.SYSTEM_PROMPT + """

本版不让你编写日期字符串或重抄凭据。资料附带 date_catalog，由宿主逐字抽取；它不是答案表，
其中也可能有旧错误和其他事情的日期。你只选支持当前这件事的 t 编号，不能因值相近就借用。
本版用 event.status 表示前述 event_status，用 event.date_ref 取代两个日期字段。
happened 的 date_ref 属于已经发生的那项行动；planned/cancelled 的 date_ref 只属于原定活动日。
取消决定的日期不能放进 cancelled.date_ref：它可保留在文字中，或另列 happened 的取消决定事实。
其他状态 date_ref 必须 null。年份不明或日期不明也用 null，保留原有月日或相对时间。
事实 text 中其他完整日期用 time_evidence 的 t 编号支持；旧说法中的完整日期用 history_time_evidence。
字段 date_ref 自身已经算当前凭据，不用在 time_evidence 再重复列。没有完整日期时列表为空。
每个 t 编号所列 evidence_ids 必须在对应事实或旧话的 evidence_ids 中，确保主语、时间和动作一起回查。
date_catalog 的 quote 是原文中该日期所在句，不代表日期角色已经被确认；务必读邻文。
不要从已被更正的旧时间引用，为另一件事补年；原文只有月日时，摘要正文也只保留月日。
同一事件自己的后续变化放在该事件内，不建指向自己的 relation；只列确有来源的跨事件关系。
不要因为增加时间核对而丢弃末尾的称呼、双方表达、感受、否定和未决事项。只交最终 JSON。
"""


def date_catalog(records, catalog):
    dates = {}
    for row in records:
        text = row["content"]
        for match in DATES.finditer(text):
            value = canonical_date(match.group())
            if value is None:
                continue
            preceding = list(re.finditer(r"[。！？!?\n]", text[:match.start()]))
            left = preceding[-1].end() if preceding else 0
            ending = re.search(r"[。！？!?\n]", text[match.end():])
            right = match.end() + ending.end() if ending else len(text)
            sentence_bounds = (left, right)
            # Bound a very long sentence without inventing a continuation or changing the date.
            left = max(left, match.start() - 200)
            right = min(right, match.end() + 200)
            refs = [sid for sid, span in catalog.items() if span["record_id"] == row["record_id"]
                    and span["start"] < right and span["end"] > left]
            dates[f"t{len(dates)+1}"] = {"date": value, "literal": match.group(), "record_id": row["record_id"],
                "date_start": match.start(), "date_end": match.end(), "start": left, "end": right,
                "quote": text[left:right], "context_truncated": (left, right) != sentence_bounds, "evidence_ids": refs}
    return dates


def prepare_author_window(records):
    prepared = previous.prepare_author_window(records)
    dates = date_catalog(records, prepared["catalog"])
    prepared.update(recipe=RECIPE, date_catalog=dates)
    prepared["messages"][0]["content"] = SYSTEM_PROMPT
    payload = json.loads(prepared["messages"][1]["content"])
    payload["date_catalog"] = dates
    prepared["messages"][1]["content"] = json.dumps(payload, ensure_ascii=False)
    fact = prepared["response_schema"]["properties"]["events"]["items"]["properties"]["facts"]["items"]
    for key in ("event_date", "planned_date", "event_status"):
        del fact["properties"][key]
    selector = {"type": ["string", "null"], "enum": [None, *dates]}
    fact["properties"]["event"] = {"anyOf": [
        _object({"status": {"type": "string", "enum": ["happened", "planned", "cancelled"]}, "date_ref": selector}),
        _object({"status": {"type": "string", "enum": ["not_happened", "not_applicable", "unknown"]},
                 "date_ref": {"type": "null"}})]}
    for key in ("time_evidence", "history_time_evidence"):
        fact["properties"][key] = {"type": "array", "maxItems": 6 if dates else 0,
            "uniqueItems": True, "items": {"type": "string", "enum": list(dates) or ["no_date_available"]}}
    fact["required"] = list(fact["properties"])
    return prepared


def inspect_and_project(raw, records, catalog):
    prepared = prepare_author_window(records)
    if prepared["catalog"] != catalog:
        raise SummaryError("date_index_catalog_changed")
    if list(Draft202012Validator(prepared["response_schema"]).iter_errors(raw)):
        raise SummaryError("date_index_invalid_shape")
    dates = prepared["date_catalog"]
    draft = copy.deepcopy(raw)
    errors, entries, current_uses, obsolete = [], [], [], set()
    for event in draft["events"]:
        for index, fact in enumerate(event["facts"], 1):
            item = f"{event['event_id']}.f{index}"
            when = fact.pop("event")
            chosen = when["date_ref"]
            value = dates[chosen]["date"] if chosen else None
            fact.update(event_status=when["status"], event_date=value if when["status"] == "happened" else None,
                        planned_date=value if when["status"] in ("planned", "cancelled") else None)
            current = list(dict.fromkeys(([chosen] if chosen else []) + fact.pop("time_evidence")))
            history = fact.pop("history_time_evidence")
            current_uses += [(item, tid) for tid in current]
            old = fact["supersedes"]
            # Only a different explicit corrected date makes an old date occurrence obsolete.
            if old and chosen:
                obsolete.update(tid for tid in history if dates[tid]["date"] != value)
            for historical, ids, text, refs in (
                (False, current, fact["text"], fact["evidence_ids"]),
                (True, history, old["text"] if old else "", old["evidence_ids"] if old else [])):
                supported = set()
                needed = _explicit_dates(text) | ({value} if not historical and value else set())
                for tid in ids:
                    source = dates[tid]
                    valid = set(source["evidence_ids"]) <= set(refs)
                    if not valid:
                        errors.append({"code": "date_index_missing_fact_reference", "item_id": item, "date_ref": tid})
                    else:
                        supported.add(source["date"])
                    if source["date"] not in needed:
                        errors.append({"code": "date_index_unclaimed_date", "item_id": item, "date_ref": tid})
                    entries.append({"item_id": item, "historical": historical, "date_ref": tid,
                        **copy.deepcopy(source), "mechanically_valid": valid, "semantic_passed": None})
                for missing in sorted(needed - supported):
                    errors.append({"code": "date_index_required", "item_id": item, "date": missing, "historical": historical})
    for item, tid in current_uses:
        if tid in obsolete:
            errors.append({"code": "date_index_uses_superseded_occurrence", "item_id": item, "date_ref": tid})
    return draft, {"schema": "echo-summary-date-index-audit-v1", "author_recipe": RECIPE,
        "raw_draft_sha256": reading.fingerprint(raw), "projected_draft_sha256": reading.fingerprint(draft),
        "source_window_sha256": reading.fingerprint(records), "entries": entries, "errors": errors,
        "semantic_passed": None, "production_accepted": False,
        "notice": "日期和引文由原文提取；选择是否属于当前事件及时间角色仍须语义复核。"}
