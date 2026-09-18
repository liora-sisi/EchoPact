"""Local-only recall diagnostics over the production read-only pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .adaptive_recall import adaptive_recall
from .speech_query_rescue import (
    SpeechQueryMapping,
    SpeechQueryRescuePolicy,
)


RECALL_DEBUG_PANEL_SCHEMA_VERSION = "echo-pact-recall-debug-panel-v1"
_SOURCE_LABELS = {
    "chatgpt": "GPT库",
    "codex_local": "Codex库",
    "other": "其他来源",
}
_SOURCE_KIND_LABELS = {
    "room-ferry-backup-v1": "GPT库（官方数据包）",
    "guijian-browser-capture-v1": "GPT库（浏览器抓取）",
    "codex-app-server-v1": "Codex库",
}


def load_speech_query_policy(path: str) -> SpeechQueryRescuePolicy:
    """Load a private, caller-owned M8 overlay without persisting it."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("speech policy must be a JSON object")
    raw_mappings = payload.get("mappings")
    if not isinstance(raw_mappings, list):
        raise ValueError("speech policy mappings must be a list")
    mappings = []
    for index, item in enumerate(raw_mappings):
        if not isinstance(item, Mapping):
            raise ValueError(f"speech mapping {index} must be an object")
        mappings.append(
            SpeechQueryMapping(
                observed=item.get("observed"),
                intended=item.get("intended"),
            )
        )
    protected_terms = payload.get("protected_terms", [])
    if not isinstance(protected_terms, list) or not all(
        isinstance(item, str) for item in protected_terms
    ):
        raise ValueError("speech policy protected_terms must be a string list")
    max_candidates = payload.get("max_candidates", 2)
    return SpeechQueryRescuePolicy(
        mappings=tuple(mappings),
        protected_terms=tuple(protected_terms),
        max_candidates=max_candidates,
    )


def run_recall_debug(
    query: str,
    *,
    db_path: str,
    agent_id: str,
    limit: int = 5,
    as_of: Optional[str] = None,
    include_projection: bool = True,
    speech_query_policy: Optional[SpeechQueryRescuePolicy] = None,
) -> Dict[str, Any]:
    """Run the real adaptive retriever with an opt-in local debug trace."""

    response = adaptive_recall(
        query,
        agent_id=agent_id,
        limit=limit,
        as_of=as_of,
        db_path=db_path,
        read_only=True,
        include_projection=include_projection,
        speech_query_policy=speech_query_policy,
        debug=True,
    )
    response["debug_panel_schema_version"] = RECALL_DEBUG_PANEL_SCHEMA_VERSION
    return response


def _source_label(channel: object, source_kind: object = None) -> str:
    kind_label = _SOURCE_KIND_LABELS.get(str(source_kind or ""))
    if kind_label:
        return kind_label
    return _SOURCE_LABELS.get(str(channel or "other"), "其他来源")


def _join_terms(values: object) -> str:
    if not isinstance(values, list):
        return "—"
    rendered = [str(item) for item in values if str(item).strip()]
    return "、".join(rendered) if rendered else "—"


def render_recall_debug(response: Mapping[str, Any]) -> str:
    """Render one compact Chinese CLI panel for non-technical inspection."""

    debug = response.get("recall_debug") or {}
    lines = [
        "Echo Pact｜召回调试面板",
        f"原始问题：{response.get('query') or '—'}",
        f"最终路径：{response.get('recall_mode') or '—'}",
    ]
    engine = debug.get("engine") or {}
    if engine:
        lines.append(
            "真实打分口径：SQLite词面/意图排序 + 有界扩查；"
            "不使用旧 semantic/emotion/importance/decay/saga 加权公式"
        )

    speech = response.get("speech_query_rescue")
    lines.append("")
    lines.append("一、Query 与错字救援（M8）")
    if not isinstance(speech, Mapping):
        lines.append("- 本次未加载私人错字映射；只运行原始 Query。")
    else:
        lines.append(f"- 原串：{speech.get('original_query') or response.get('query')}")
        candidates = speech.get("candidates") or []
        if not candidates:
            lines.append(
                f"- 未扩查：{speech.get('trigger_reason') or '没有符合条件的候补'}"
            )
        for candidate in candidates:
            lines.append(
                "- 候补："
                f"{candidate.get('candidate_query')}"
                f"（{candidate.get('observed')} → {candidate.get('intended')}）"
            )

    lines.append("")
    lines.append("二、实际召回轮次")
    passes = debug.get("passes") or []
    if not passes:
        lines.append("- 没有产生可展示的召回轮次。")
    for item in passes:
        plan = item.get("query_plan") or {}
        lines.append(
            f"- {item.get('pass')}｜{item.get('recall_mode')}｜"
            f"候选 {item.get('candidate_count', 0)} 条"
        )
        lines.append(f"  查询：{item.get('query') or '—'}")
        lines.append(
            "  核心词："
            + _join_terms(
                list(plan.get("explicit_literal_anchors") or [])
                + list(plan.get("focused_like_terms") or [])
            )
        )

    lines.append("")
    lines.append("三、Top-K 结果")
    top_k = debug.get("top_k") or []
    if not top_k:
        lines.append("- 没有召回结果。")
    provenance = response.get("source_provenance") or {}
    mirror_groups = provenance.get("exact_cross_source_mirror_groups") or []
    mirror_by_representative = {
        str(group.get("representative_record_id")): group
        for group in mirror_groups
        if group.get("representative_record_id")
    }
    for item in top_k:
        retrieval = item.get("retrieval_debug") or {}
        lines.append(
            f"[{item.get('position')}] "
            f"{_source_label(item.get('source_channel'), item.get('source_kind'))}｜"
            f"{item.get('record_id')}｜证据分 {item.get('confidence')}"
        )
        lines.append(
            "  依据：" + "；".join(item.get("confidence_basis") or ["—"])
        )
        if retrieval.get("lexical_rank") is not None:
            lines.append(
                f"  词面序值：{retrieval.get('lexical_rank')} "
                f"（{retrieval.get('rank_semantics')}）"
            )
        lines.append(
            f"  最终排序：本轮第 {retrieval.get('position_in_pass') or item.get('position')} 名；"
            "当前引擎没有统一加权总分"
        )
        lines.append(
            "  命中轮次：" + _join_terms(item.get("adaptive_match_passes"))
        )
        mirror = mirror_by_representative.get(str(item.get("record_id")))
        if mirror:
            lines.append(
                f"  镜像收敛：并入本条 {len(mirror.get('compacted_record_ids') or [])} 条"
            )
        lines.append(f"  内容：{item.get('content_preview') or '—'}")

    lines.append("")
    lines.append("四、剔除说明")
    exclusions = debug.get("exclusions") or []
    if not exclusions:
        clarification = response.get("query_clarification") or {}
        temporal = response.get("temporal_coverage") or {}
        if clarification.get("status") == "required":
            lines.append("- Query缺少明确主题，候选在进入公开结果前被整体压住。")
        elif temporal.get("coverage_gap"):
            lines.append("- 查询时间超出当前数据覆盖范围，旧记录没有冒充新证据。")
        else:
            lines.append("- 本次没有记录被 Top-K 或镜像规则剔除。")
    for item in exclusions:
        source = _source_label(item.get("source_channel"), item.get("source_kind"))
        record_id = item.get("record_id") or "同记录重复命中"
        target = item.get("merged_into_record_id")
        target_position = item.get("merged_into_position")
        if target_position:
            suffix = f"，合并到第 {target_position} 条"
        elif target:
            suffix = f"，合并到 {target}"
        else:
            suffix = ""
        lines.append(
            f"- {source}｜{record_id}：{item.get('reason') or item.get('reason_code')}{suffix}"
        )

    lines.append("")
    lines.append(
        "说明：不可见记录会在数据库查询阶段直接挡住；面板不会泄露其正文或编号。"
    )
    return "\n".join(lines)
