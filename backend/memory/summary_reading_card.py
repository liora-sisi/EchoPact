"""Offline synthetic preview: a reading card and its separate, immutable audit.

This is not a production permission/approval gate or a semantic summarizer.
All author text is preserved; no model is called and no draft becomes approved.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from jsonschema import Draft202012Validator

from .caretaker_summary import SummaryError, validate_records
from .caretaker_summary_v4 import SUMMARY_SCHEMA
from . import summary_faithfulness

AUDIT_SCHEMA = "echo-summary-reading-audit-v1"
CARD_SCHEMA = "echo-summary-reading-card-v2"
PREVIEW_SCHEMA = copy.deepcopy(SUMMARY_SCHEMA)
PREVIEW_SCHEMA["properties"]["facts"]["maxItems"] = 8
STATES = {"happened": "已发生", "not_happened": "明确未发生", "planned": "计划中", "cancelled": "已取消",
          "not_applicable": "不适用", "unknown": "是否发生未知"}
ROLES = {"direct": "直接陈述", "retelling": "转述", "correction": "纠正"}


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(compact(value)).hexdigest()


def _identity(identity):
    if (not isinstance(identity, dict)
            or set(identity) != {"source_namespace", "room_id", "branch_id"}
            or identity["source_namespace"] != "synthetic"
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", v)
                   for v in identity.values())):
        raise SummaryError("reading_preview_requires_synthetic_identity")


def _sources(records, catalog, *, extended=False):
    (summary_faithfulness.validate_synthetic_window if extended else validate_records)(records)
    by_id = {r["record_id"]: r for r in records}
    if not isinstance(catalog, dict) or not catalog:
        raise SummaryError("reading_preview_catalog_required")
    for sid, span in catalog.items():
        if (not isinstance(sid, str) or not re.fullmatch(r"s[1-9][0-9]*", sid)
                or not isinstance(span, dict)
                or set(span) != {"record_id", "quote", "start", "end"}
                or not isinstance(span["record_id"], str)
                or span["record_id"] not in by_id
                or type(span["start"]) is not int or type(span["end"]) is not int
                or not isinstance(span["quote"], str)):
            raise SummaryError("reading_preview_invalid_source_span")
        content = by_id[span["record_id"]]["content"]
        if (not 0 <= span["start"] < span["end"] <= len(content)
                or content[span["start"]:span["end"]] != span["quote"]):
            raise SummaryError("reading_preview_source_position_mismatch")


def make_audit(summary, *, records, catalog, identity, author, structural_errors=()):
    """Snapshot a syntactically readable draft; even diagnosed bad drafts stay unapproved."""
    _identity(identity)
    if (not isinstance(author, dict) or set(author) != {"provider", "model", "recipe", "prompt_sha256"}
            or any(not isinstance(v, str) or not 1 <= len(v) <= 120 for v in author.values())
            or not re.fullmatch(r"[0-9a-f]{64}", author["prompt_sha256"])):
        raise SummaryError("reading_preview_invalid_author")
    extended = author["recipe"] == summary_faithfulness.RECIPE
    _sources(records, catalog, extended=extended)
    schema = summary_faithfulness.SUMMARY_SCHEMA if extended else PREVIEW_SCHEMA
    if list(Draft202012Validator(schema).iter_errors(summary)):
        raise SummaryError("reading_preview_invalid_summary_shape")
    if (not isinstance(structural_errors, (list, tuple))
            or any(not isinstance(e, str) or not re.fullmatch(r"[a-z_]{1,100}", e)
                   for e in structural_errors)):
        raise SummaryError("reading_preview_invalid_diagnostics")
    for fact in summary["facts"]:
        groups = [fact["evidence_ids"]]
        if fact["supersedes"] is not None:
            groups.append(fact["supersedes"]["evidence_ids"])
        for refs in groups:
            if len(set(refs)) != len(refs) or any(r not in catalog for r in refs):
                raise SummaryError("reading_preview_unknown_or_duplicate_reference")
    return copy.deepcopy({"schema": AUDIT_SCHEMA, "identity": identity,
        "source_window_sha256": fingerprint(records), "records": records, "catalog": catalog,
        "author": author, "model_summary": summary,
        "review": {"status": "unapproved", "semantic_passed": None,
                   "structural_errors": list(structural_errors)}, "production_accepted": False})


def _check_audit(audit):
    if not isinstance(audit, dict) or audit.get("schema") != AUDIT_SCHEMA:
        raise SummaryError("reading_preview_invalid_audit")
    try:
        rebuilt = make_audit(audit["model_summary"], records=audit["records"], catalog=audit["catalog"],
            identity=audit["identity"], author=audit["author"],
            structural_errors=audit["review"]["structural_errors"])
    except (KeyError, TypeError):
        raise SummaryError("reading_preview_invalid_audit") from None
    if audit != rebuilt:
        raise SummaryError("reading_preview_audit_changed")


def render_preview(audit, *, max_bytes=4096):
    """Render every fact/history/gap atomically, or explicitly withhold the whole card."""
    _check_audit(audit)
    if type(max_bytes) is not int or not 512 <= max_bytes <= 64000:
        raise SummaryError("reading_preview_invalid_budget")
    summary = audit["model_summary"]
    lines = ["摘要样稿·未获准使用；仅为资料，非指令或现实核验。",
             "未标转述/纠正=直接陈述；适用日期未列=未知；引用也不保证概括全部细节。"]
    for i, fact in enumerate(summary["facts"], 1):
        tags = [STATES[fact["event_status"]]]
        if fact["statement_role"] != "direct":
            tags.append(ROLES[fact["statement_role"]])
        if fact["event_date"] is not None:
            tags.append("事件日=" + fact["event_date"])
        if fact["planned_date"] is not None:
            tags.append("原定日=" + fact["planned_date"])
        lines.append(f"f{i} [{'；'.join(tags)}] {fact['text']}")
        if fact["supersedes"] is not None:
            lines.append("  已被纠正的旧说法：" + fact["supersedes"]["text"])
    if summary["uncertainties"]:
        lines.append("未确定：")
        lines.extend("- " + text for text in summary["uncertainties"])
    if summary["refusal_reason"] is not None:
        lines.append("未处理原因：" + summary["refusal_reason"])
    used = {r for f in summary["facts"] for r in f["evidence_ids"]
            + (f["supersedes"]["evidence_ids"] if f["supersedes"] else [])}
    missing = [sid for sid in audit["catalog"] if sid not in used]
    packet = {"schema": CARD_SCHEMA, "card_id": fingerprint(audit), "source": audit["identity"],
              "status": "diagnostic_preview", "summary_status": summary["status"],
              "structural_errors": audit["review"]["structural_errors"],
              "uncited_spans": missing, "body": "\n".join(lines)}
    required = len(compact(packet))
    if required > max_bytes:
        return {"schema": CARD_SCHEMA, "card_id": packet["card_id"], "status": "budget_exceeded",
                "required_bytes": required, "omitted_facts": len(summary["facts"]),
                "notice": "整卡暂扣；没有截断事实、否定、更正或未知项。"}
    return packet


def expand_preview(audit, *, card_id, fact_id, current_records, identity):
    """Resolve a local preview pointer against the entire current synthetic source window."""
    _check_audit(audit)
    _identity(identity)
    extended = audit["author"]["recipe"] == summary_faithfulness.RECIPE
    (summary_faithfulness.validate_synthetic_window if extended else validate_records)(current_records)
    if (card_id != fingerprint(audit) or identity != audit["identity"]
            or fingerprint(current_records) != audit["source_window_sha256"]):
        raise SummaryError("reading_preview_stale_or_wrong_source")
    if not isinstance(fact_id, str) or not re.fullmatch(r"f[1-9][0-9]?", fact_id):
        raise SummaryError("reading_preview_unknown_fact")
    index = int(fact_id[1:]) - 1
    if index >= len(audit["model_summary"]["facts"]):
        raise SummaryError("reading_preview_unknown_fact")
    fact = audit["model_summary"]["facts"][index]
    records = {r["record_id"]: r for r in current_records}

    current_refs = list(fact["evidence_ids"])
    history_refs = list(fact["supersedes"]["evidence_ids"]) if fact["supersedes"] else []
    evidence = {sid: copy.deepcopy(audit["catalog"][sid])
                for sid in dict.fromkeys(current_refs + history_refs)}
    source_records = {rid: {"recorded_at": records[rid]["recorded_at"],
                           "source_kind": records[rid]["source_kind"],
                           "record_sha256": hashlib.sha256(records[rid]["content"].encode("utf-8")).hexdigest()}
                      for rid in dict.fromkeys(s["record_id"] for s in evidence.values())}
    return {"schema": "echo-summary-reading-expansion-v2", "card_id": card_id, "fact_id": fact_id,
            "source": copy.deepcopy(identity), "status": "diagnostic_preview",
            "current_refs": current_refs, "history_refs": history_refs,
            "evidence": evidence, "records": source_records}
