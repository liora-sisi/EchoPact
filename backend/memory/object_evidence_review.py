"""Source-ordered context for the isolated object scout, with no transport.

Only verified local adjacency becomes an anonymous order window. Array ranking,
message timestamps, lexical correction markers and model guesses never do.
"""
from __future__ import annotations

import copy
import re

from . import evidence_selector as selection
from . import object_recall_shadow as scout

VERSION = "echo-object-evidence-review-v4"
MAX_WINDOWS = 24
MAX_ORDER_CONTEXT_ITEMS = 6  # Separate from six model selections; same whole-packet byte cap.
SYSTEM_PROMPT = selection.SYSTEM_PROMPT + """
本次还可能有 source_order_windows。每组 candidate_ids 是本机按同一原始对话分支位置核验的发言先后。
组内从先到后；不同组之间没有给出全局先后，可能是不同分支。候选数组和编号只表示展示顺序，绝不是时间。
这只证明记录的先后，不证明事情发生的先后，不证明后一句必然正确。同一候选可出现在重叠窗口。
明确针对同一对象、同一事实作出的修订，可以解释旧稿为何错误；当替代关系清楚且没有未解决的新冲突时，
当前更正标 answers，已被明确替代的旧稿标 context，不必把已解决的纠正一律当成冲突。
如果两次都明确是在修订当前同一事实，并且在同一窗口内有先后，可按后一次明确修订判当前描述。
但后来抄录旧稿、回忆过去、转述他人都不能冒充新的事实修订。助手重复错误或说“已改好”本身也不能证明改对了。
不能只按 user/assistant 身份自动判真，也不能只按更正这个词判真；要核对对象、适用范围与实际内容。
不同分支、不同对象或缺乏足够顺序证据的矛盾，仍然保留 uncertain 和相应的歧义，不能偷偷挑一边。
"""


class ReviewError(ValueError):
    pass


def validate_request(request):
    if (not isinstance(request, dict) or set(request) != {"question", "candidates", "source_order_windows"}
            or len(selection.encoded(request)) > selection.MAX_INPUT_BYTES):
        raise ReviewError("invalid_ordered_request")
    selection.planner.validate_question(request["question"])
    cards, windows = request["candidates"], request["source_order_windows"]
    if not isinstance(cards, list) or not 1 <= len(cards) <= selection.MAX_CANDIDATES:
        raise ReviewError("invalid_ordered_candidates")
    ids = set()
    for card in cards:
        if (not isinstance(card, dict) or set(card) != {"candidate_id", "source_kind", "role", "text", "excerpt_incomplete"}
                or not isinstance(card["candidate_id"], str) or not re.fullmatch(r"c[1-9][0-9]?", card["candidate_id"])
                or card["candidate_id"] in ids or not isinstance(card["text"], str)
                or not 1 <= len(card["text"]) <= selection.MAX_EXCERPT_CHARS or type(card["excerpt_incomplete"]) is not bool
                or any(not isinstance(card[k], str) or not 1 <= len(card[k]) <= 256 for k in ("source_kind", "role"))):
            raise ReviewError("invalid_ordered_candidate")
        ids.add(card["candidate_id"])
    if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
        raise ReviewError("invalid_order_windows")
    seen = set()
    for window in windows:
        if (not isinstance(window, dict) or set(window) != {"candidate_ids"}
                or not isinstance(window["candidate_ids"], list)
                or not 2 <= len(window["candidate_ids"]) <= scout.MAX_GROUP_RECORDS
                or any(not isinstance(cid, str) or cid not in ids for cid in window["candidate_ids"])
                or len(set(window["candidate_ids"])) != len(window["candidate_ids"])):
            raise ReviewError("invalid_order_window")
        ordered = tuple(window["candidate_ids"])
        if ordered in seen:
            raise ReviewError("duplicate_order_window")
        seen.add(ordered)


def prepare(scanned):
    if scanned.get("status") != "prepared_local_only" or not scanned.get("prepared"):
        raise ReviewError("successful_local_scout_required")
    prepared = copy.deepcopy(scanned["prepared"])
    lookup = {scout._identity(row): cid for cid, row in prepared["local"].items()}
    if len(lookup) != len(prepared["local"]):
        raise ReviewError("duplicate_source_identity")
    windows, seen, observed = [], set(), set()

    def candidate(row):
        identity = scout._identity(row)
        cid = lookup.get(identity)
        if cid is None:
            return None
        local = prepared["local"][cid]
        content = scout._text(row)
        if (selection._metadata(row) != selection._metadata(local)
                or selection.digest(content) != local["observed_content_sha256"]
                or content[local["excerpt_start"]:local["excerpt_start"] + len(local["text"])] != local["text"]
                or (row.get("content_truncated") is True) != local["source_truncated"]):
            raise ReviewError("source_observation_changed")
        observed.add(cid)
        return cid

    scans = scanned.get("scans", [])
    if not isinstance(scans, list) or not 1 <= len(scans) <= scout.MAX_SCANS:
        raise ReviewError("invalid_scans")
    for packet in scans:
        rows = packet.get("memories", [])
        if len(rows) > scout.SCAN_LIMIT:
            raise ReviewError("unbounded_scans")
        for parent in rows:
            parent_cid = candidate(parent)
            groups = {}
            children = parent.get("conversation_context", [])
            if len(children) > scout.MAX_CONTEXT:
                raise ReviewError("unbounded_context")
            for child in children:
                if not scout._adjacent(parent, child):
                    continue
                child_cid = candidate(child)
                if not child_cid:
                    continue
                for p in parent.get("branch_memberships", []):
                    for c in child.get("branch_memberships", []):
                        pos, other = p.get("position"), c.get("position")
                        if (p.get("branch_id") and p.get("branch_id") == c.get("branch_id")
                                and type(pos) is int and type(other) is int and 0 < abs(pos - other) <= 2
                                and c.get("relative_position") == other - pos):
                            group = groups.setdefault((p["branch_id"], pos), {})
                            if parent_cid:
                                group[parent_cid] = pos
                            group[child_cid] = other
            for group in groups.values():
                if len(group) < 2:
                    continue
                if len(set(group.values())) != len(group):
                    raise ReviewError("ambiguous_source_position")
                order = tuple(sorted(group, key=group.get))
                if order not in seen:
                    seen.add(order)
                    windows.append({"candidate_ids": list(order)})
    if observed != set(prepared["local"]):
        raise ReviewError("unobserved_candidate")
    prepared["request"]["source_order_windows"] = windows
    validate_request(prepared["request"])
    prepared["object_scout_needs_more"] = scanned.get("needs_more") is not False
    prepared["object_scan_limit_reached"] = scanned.get("scan_limit_reached") is True
    return prepared


def make_brief(draft, prepared):
    validate_request(prepared["request"])
    brief = selection.make_brief(draft, prepared)
    brief["schema_version"] = VERSION + "-brief"
    brief["source_order_windows_used"] = len(prepared["request"]["source_order_windows"])
    brief["limitations"].append("source_order_is_not_event_time_or_truth")
    brief["needs_more_evidence"] |= prepared.get("object_scout_needs_more") is not False
    _retain_order_context(brief, draft, prepared)
    if len(selection.encoded(brief)) > selection.MAX_BRIEF_BYTES:
        raise ReviewError("ordered_brief_budget_exceeded")
    return brief


def _retain_order_context(brief, draft, prepared):
    """A model's 'context' label cannot silently remove a nearby correction.

    Preserve whole connected source windows, without deciding which speaker is
    right. Companions are explicitly LOCAL context, never model-selected answers.
    Allocate complete groups in the model's original selection order. If a
    group cannot fit, withhold it; later singleton hits cannot displace the
    companions of an earlier selected group. Do not interpret ranking as time.
    """
    local = prepared["local"]
    lookup = {scout._identity(row): cid for cid, row in local.items()}
    judgments = {j["candidate_id"]: j for j in draft["judgments"] if j["relation"] in ("answers", "uncertain")}
    selected = set(judgments)
    priority = {cid: index for index, cid in enumerate(judgments)}
    components = [{cid} for cid in local]
    groups = [set(w["candidate_ids"]) for w in prepared["request"]["source_order_windows"]]
    conflicts = {r.get("conflict_group_id") for r in local.values()} - {None, ""}
    groups.extend({cid for cid, row in local.items() if row.get("conflict_group_id") == conflict}
                  for conflict in conflicts)
    for group in groups:
        touching = [c for c in components if c & group]
        components = [c for c in components if not c & group] + [set.union(group, *touching)]
    order = {cid: index for index, cid in enumerate(local)}
    components = [group for group in components if group & selected]
    components.sort(key=lambda group: min(priority[cid] for cid in group & selected))
    brief["items"] = []
    brief["source_order_context"] = []
    brief["order_context_groups_withheld"] = 0
    brief["partial_groups_withheld"] = 0
    brief["limitations"].append("local_order_context_is_not_an_answer_or_a_truth_judgment")

    def source_item(cid, *, companion=False):
        source = local[cid]
        if companion:
            quote, relative, method, relation = source["text"], 0, "local_exact_order_context", "context"
        else:
            quote, relative, method = selection.resolve_quote(source["text"], judgments[cid]["quote"])
            relation = judgments[cid]["relation"]
        ref = {k: source[k] for k in ("record_id", "source_kind", "conversation_id", "role",
            "created_at", "conflict_group_id", "observed_content_sha256", "source_truncated")}
        start = source["excerpt_start"] + relative
        ref.update(quote_start=start, quote_end=start + len(quote), quote_resolution=method)
        return {"text": quote, "relation": relation, "references": [ref]}

    def display_key(item):
        ref = item["references"][0]
        return None if ref["source_truncated"] else (
            ref["observed_content_sha256"], ref["created_at"], ref["role"], ref["conflict_group_id"],
            item["relation"], item["text"])

    for group in components:
        wanted = group & selected
        companions = group - selected
        proposed = copy.deepcopy(brief)
        for cid in sorted(wanted, key=priority.get):
            item = source_item(cid)
            key = display_key(item)
            same = next((old for old in proposed["items"] if key is not None and display_key(old) == key), None)
            if same is None:
                proposed["items"].append(item)
            else:
                same["references"].extend(item["references"])
        if companions:
            proposed["source_order_context"].append({
                "for_record_ids": [local[cid]["record_id"] for cid in sorted(wanted, key=order.get)],
                "basis": "verified_source_order_windows_not_semantic_truth",
                "source_order_windows": [{"record_ids": [local[cid]["record_id"] for cid in w["candidate_ids"]]}
                    for w in prepared["request"]["source_order_windows"] if set(w["candidate_ids"]) <= group],
                "items": [source_item(cid, companion=True) for cid in sorted(companions, key=order.get)]})
        context_count = sum(len(b["items"]) for b in proposed["source_order_context"])
        if (len(proposed["items"]) > selection.MAX_ITEMS or context_count > MAX_ORDER_CONTEXT_ITEMS
                or len(selection.encoded(proposed)) > selection.MAX_BRIEF_BYTES - 256):
            brief["partial_groups_withheld"] += int(len(wanted) > 1)
            brief["order_context_groups_withheld"] += int(bool(companions))
            brief["needs_more_evidence"] = True
            continue
        brief["items"] = proposed["items"]
        brief["source_order_context"] = proposed["source_order_context"]
    visible = {lookup[scout._identity(ref)] for item in brief["items"] for ref in item["references"]}
    brief["omitted_selected"] = len(selected - visible)
    brief["duplicate_display_count"] = sum(len(i["references"]) - 1 for i in brief["items"])
    brief["needs_more_evidence"] |= bool(brief["omitted_selected"] or brief["source_order_context"])
    if brief["source_order_context"] or brief["order_context_groups_withheld"]:
        brief["evidence_status"] = "model_judgment_with_local_order_context_review_required"


def refine_object_shadow(scanned, *, fallback_brief, reviewer=None):
    """One explicitly injected review call; failure preserves the older brief."""
    result = {"schema_version": VERSION, "status": "disabled", "review_attempts": 0,
              "brief": copy.deepcopy(fallback_brief)}
    if reviewer is None:
        return result
    try:
        prepared = prepare(scanned)
        result["prepared"] = prepared  # Local audit only, not the client-facing brief.
        result["review_attempts"] = 1
        draft = reviewer(copy.deepcopy(prepared["request"]))
        result["brief"] = make_brief(draft, prepared)
        result.update(status="selected_requires_review", judgments=copy.deepcopy(draft))
    except Exception:
        result["status"] = "failed_original_brief_preserved"
    return result
