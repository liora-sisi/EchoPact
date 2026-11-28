"""Versioned author contract; legacy event audits and originals stay unchanged."""
from __future__ import annotations

import copy

from . import summary_event_booklet as book
from .caretaker_summary_v2 import _explicit_dates

RECIPE = "echo-summary-event-author-v2"
SYSTEM_PROMPT = book.SYSTEM_PROMPT + """
出处规则：所有 evidence_ids（事实、更正历史、未知、关系）只选当前资料 spans.id 中的 s 编号。
record_id 是整条记录编号，只用于 gaps.record_ids；它不是 evidence_ids。前后分段共同支持一项事实时都引用。

状态规则：happened 是明确叙述已做的行动或一次表达；not_happened 是截至所述时点明确尚未发生；
planned 是仍有效的打算；cancelled 是已取消的打算；not_applicable 是持续感受、偏好、关系等；
unknown 才是资料不能确定。不同状态的行动分开，不能用未知清单代替明确否定。
日期规则同样约束 text：来源只说月日时，文字也保留月日，不能借窗口其他事情的年份补齐。
需要完整日期的事实必须引用明确支持这个日期和这个事件的出处；存在一个同日期不等于属于这件事。

statement_role 的 direct 是本次直接陈述，retelling 是叙述者转述别人的话。
correction 仅用于明确改正来源中确实出现过的旧断言；supersedes 必须有真实旧话支撑。
“不要误写成某事”“这不代表某事”只是防止误读，不证明以前真的有人说过那件事。
回答问题、否定猜测、改动日后安排，也不自动成为纠正旧断言；保留问题/猜测/历史计划原有语气。
非 correction 的 supersedes=null。真正更正的当前 evidence_ids 包含旧出处和新出处，
supersedes.evidence_ids 只含支持旧话的出处；不能用新话倒造不存在的旧话。

同一事件的当前结论、变更经过、否定和未知放在同一事件内；末尾复述同一结论时合并引用，
不再单独写一个重复所有事件的“最终状态”事件。明确的历史经过可保留，不能冒充当前有效状态。
relations 只写原文明确支持的事件联系；仅仅出现在同一窗口不构成 related。
这些都是通用写作规则，不是答题线索。只交最终 JSON，不输出检查过程。
"""


def prepare_author_window(records):
    prepared = book.prepare_author_window(records)
    prepared["recipe"] = RECIPE
    prepared["messages"][0]["content"] = SYSTEM_PROMPT
    schema = copy.deepcopy(prepared["response_schema"])
    span_ids = list(prepared["catalog"])
    record_ids = [row["record_id"] for row in records]

    def bind(node):
        if isinstance(node, dict):
            props = node.get("properties", {})
            if "evidence_ids" in props:
                props["evidence_ids"]["items"] = {"type": "string", "enum": list(span_ids)}
            if "record_ids" in props:
                props["record_ids"]["items"] = {"type": "string", "enum": list(record_ids)}
            for value in node.values():
                bind(value)
        elif isinstance(node, list):
            for value in node:
                bind(value)

    bind(schema)
    prepared["response_schema"] = schema
    return prepared


def inspect_text_dates(draft, catalog):
    """Catch literal date overreach, without changing or semantically approving text."""
    errors = []

    def check(item, code, **location):
        refs = item["evidence_ids"]
        # Invalid references are already blocked by the host audit.
        supported = set().union(*(_explicit_dates(catalog[r]["quote"]) for r in refs))
        if _explicit_dates(item["text"]) - supported:
            errors.append({"code": code, **location})

    for event in draft["events"]:
        for index, fact in enumerate(event["facts"]):
            loc = {"event_id": event["event_id"], "fact_index": index}
            check(fact, "text_date_without_current_evidence", **loc)
            if fact["supersedes"]:
                check(fact["supersedes"], "history_text_date_without_history_evidence", **loc)
        for index, unknown in enumerate(event["uncertainties"]):
            check(unknown, "uncertainty_date_without_evidence", event_id=event["event_id"], uncertainty_index=index)
    for index, unknown in enumerate(draft["uncertainties"]):
        check(unknown, "uncertainty_date_without_evidence", uncertainty_index=index)
    return errors
