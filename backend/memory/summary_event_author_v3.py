"""Exact date witnesses for synthetic event drafts, never semantic auto-approval."""
from __future__ import annotations

import copy

from jsonschema import Draft202012Validator

from .caretaker_summary import SummaryError, _object
from .caretaker_summary_v2 import _explicit_dates
from .summary_faithfulness import canonical_date
from . import summary_event_author_v2 as previous
from . import summary_reading_card as reading

RECIPE = "echo-summary-event-author-v3-date-witness"
SYSTEM_PROMPT = previous.SYSTEM_PROMPT + """

本版给事实中的完整日期增加 time_evidence；被更正旧话中的完整日期另用 history_time_evidence。
每个完整年月日（日期字段或 text 中的日期）都须有对应凭据；没有完整日期时列表为空。
一项凭据包括：date_text（照抄原文完整日期）、event_phrase（照抄描述那件事的短语）、
quote（照抄最短但足够的连续原句）、evidence_ids（原句所在片段）。不要改写 quote。
event_phrase 也须出现在本条事实 text 中；用行动或对象短语，不能仅用人名、日期或“这件事”。
凭据的引用必须已在对应事实/旧话的 evidence_ids 中。跨片段原句只能拼同一记录的连续片段。
每段 quote 只能有一种完整日期；如果夹带其他完整日期，说明选得太宽，重新缩小原句。
原文同时谈多件事时，确认 date_text 和 event_phrase 确实属于同一件事，不借旧错误或邻事日期。
无法给出这样的凭据，就不要填完整日期；保留原有月日或相对时间，不删掉已知事实来躲检查。
只写月日的原文不能补年；取消的决定日不是原定活动日。正文中的完整日期也遵守这些要求。
同一条有购票日期和出发日期时分别给出处，或拆成真正不同的事实，不混淆两件事的时间。
time_evidence 是后台复核材料，不是给主助手阅读的正文；不要在 text 重复抄凭据。
这不是让你自行宣布通过。保留全部重要称呼、双方表达、否定、更正和未知，只交最终 JSON。
"""


def prepare_author_window(records):
    prepared = previous.prepare_author_window(records)
    prepared["recipe"] = RECIPE
    prepared["messages"][0]["content"] = SYSTEM_PROMPT
    refs = {"type": "array", "minItems": 1, "maxItems": 5, "uniqueItems": True,
            "items": {"type": "string", "enum": list(prepared["catalog"])}}
    witness = _object({"date_text": {"type": "string", "minLength": 8, "maxLength": 20},
        "event_phrase": {"type": "string", "minLength": 2, "maxLength": 60},
        "quote": {"type": "string", "minLength": 8, "maxLength": 480}, "evidence_ids": refs})
    fact = prepared["response_schema"]["properties"]["events"]["items"]["properties"]["facts"]["items"]
    for name in ("time_evidence", "history_time_evidence"):
        fact["properties"][name] = {"type": "array", "maxItems": 6, "items": copy.deepcopy(witness)}
        fact["required"].append(name)
    return prepared


def inspect_and_project(raw_draft, records, catalog):
    """Retain witnesses separately; project unchanged facts for legacy card layout."""
    prepared = prepare_author_window(records)
    if prepared["catalog"] != catalog:
        raise SummaryError("date_witness_catalog_changed")
    if list(Draft202012Validator(prepared["response_schema"]).iter_errors(raw_draft)):
        raise SummaryError("date_witness_invalid_shape")
    draft = copy.deepcopy(raw_draft)
    errors, entries = [], []

    def error(code, item, **extra):
        errors.append({"code": code, "item_id": item, **extra})

    def inspect(witnesses, *, text, refs, values, item, history):
        needed = _explicit_dates(text) | {canonical_date(value) for value in values if value is not None}
        needed.discard(None)
        supported = set()
        for index, w in enumerate(witnesses):
            before_errors = len(errors)
            canonical = canonical_date(w["date_text"])
            parts = [catalog[sid] for sid in w["evidence_ids"]]
            ordered = sorted(parts, key=lambda p: p["start"])
            if (len({p["record_id"] for p in parts}) != 1
                    or any(a["end"] != b["start"] for a, b in zip(ordered, ordered[1:]))):
                error("date_witness_noncontiguous_or_cross_record", item, witness_index=index)
                continue
            quote = w["quote"]
            source_text = "".join(p["quote"] for p in ordered)
            if quote not in source_text:
                error("date_witness_quote_not_exact", item, witness_index=index)
            elif source_text.count(quote) != 1:
                error("date_witness_quote_position_ambiguous", item, witness_index=index)
            if not set(w["evidence_ids"]) <= set(refs):
                error("date_witness_missing_fact_reference", item, witness_index=index)
            if canonical is None or w["date_text"] not in quote or _explicit_dates(quote) != {canonical}:
                error("date_witness_ambiguous_or_invalid_date", item, witness_index=index)
            phrase = w["event_phrase"].strip()
            if not phrase or phrase not in quote or phrase not in text or _explicit_dates(phrase):
                error("date_witness_event_phrase_not_shared", item, witness_index=index)
            if canonical not in needed:
                error("date_witness_unclaimed_date", item, witness_index=index)
            if canonical is not None and len(errors) == before_errors:
                supported.add(canonical)
            # Position is derived locally from the exact source, not model-supplied.
            offset = source_text.find(quote)
            entries.append({"item_id": item, "historical": history, "witness_index": index,
                "date": canonical, **copy.deepcopy(w), "record_id": ordered[0]["record_id"],
                "start": ordered[0]["start"] + offset if offset >= 0 else None,
                "end": ordered[0]["start"] + offset + len(quote) if offset >= 0 else None,
                "mechanically_valid": len(errors) == before_errors, "semantic_passed": None})
        for missing in sorted(needed - supported):
            error("date_witness_required", item, date=missing, historical=history)

    for event in draft["events"]:
        for index, fact in enumerate(event["facts"], 1):
            item = f"{event['event_id']}.f{index}"
            inspect(fact.pop("time_evidence"), text=fact["text"], refs=fact["evidence_ids"],
                    values=(fact["event_date"], fact["planned_date"]), item=item, history=False)
            old = fact["supersedes"]
            witnesses = fact.pop("history_time_evidence")
            inspect(witnesses, text=old["text"] if old else "", refs=old["evidence_ids"] if old else [],
                    values=(), item=item, history=True)
    return draft, {"schema": "echo-summary-date-witness-audit-v1", "author_recipe": RECIPE,
        "raw_draft_sha256": reading.fingerprint(raw_draft), "projected_draft_sha256": reading.fingerprint(draft),
        "source_window_sha256": reading.fingerprint(records), "entries": entries, "errors": errors,
        "semantic_passed": None, "production_accepted": False,
        "notice": "原句、日期和短语共现不证明语义归属；仍须核对同一事件、时间角色和旧话。"}
