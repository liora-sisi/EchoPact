"""Local-only object comparison scout over a frozen, readable archive.

This opt-in experiment broadens local candidate inspection, not model input.
Source labels are search clues, never an identification of the question's pair.
It has no model client and is not imported by the production recall routes.
"""
from __future__ import annotations

import copy
import re

from . import evidence_selector as selection
from . import query_caretaker as planner
from .named_collection import public_subject_aliases

VERSION = "echo-object-recall-shadow-v1"
MAX_SCANS = 2
SCAN_LIMIT = 48
MAX_CONTEXT = 4
MAX_SCAN_CHARS = 8000
MAX_GROUP_RECORDS = 5
MAX_CLUES_PER_RECORD = 8
_TITLE = re.compile(r"《([A-Za-z0-9\u3400-\u9fff ·_—-]{2,24})》")
_CORRECTION = re.compile(r"串线|混淆|搞混|弄混|写错|更正|不是.{0,40}(?:而是|是)|correction\b", re.I)


class ObjectRecallError(ValueError):
    pass


def readonly_runner(db_path, agent_id):
    """Bind the identity and read-only mode outside any question/source text."""
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ObjectRecallError("fixed_agent_required")

    def run(arguments):
        from .records_v1 import recall_records
        if set(arguments) != {"query", "limit", "as_of"} or arguments["limit"] != SCAN_LIMIT:
            raise ObjectRecallError("invalid_scan_arguments")
        return recall_records(**arguments, db_path=db_path, agent_id=agent_id, read_only=True)

    return run


def _identity(row):
    meta = selection._metadata(row)
    if not meta["conversation_id"]:
        raise ObjectRecallError("missing_conversation")
    return meta["source_kind"], meta["conversation_id"], meta["record_id"]


def _text(row):
    text = row.get("content")
    if not isinstance(text, str):
        raise ObjectRecallError("invalid_source_content")
    if row.get("content_truncated") is True:
        text = text.removesuffix("\n[Echo Pact: content truncated]")
    return text


def _seed_labels(text, aliases):
    labels = []
    for match in _TITLE.finditer(text):
        # A nearby explicit category links the title to this inventory. A title
        # in a distant diary section cannot borrow the category from a heading.
        nearby = text[max(0, match.start() - 24):match.end() + 24]
        if any(term in nearby for term in aliases) and match.group(1) not in labels:
            labels.append(match.group(1))
    return labels[:MAX_CLUES_PER_RECORD]


def _adjacent(parent, child):
    """Require the gateway's actual branch positions, not date proximity."""
    if any(parent.get(k) != child.get(k) for k in ("source_kind", "conversation_id")):
        return False
    for p in parent.get("branch_memberships", []):
        for c in child.get("branch_memberships", []):
            pos, other = p.get("position"), c.get("position")
            if (p.get("branch_id") and p.get("branch_id") == c.get("branch_id")
                    and type(pos) is int and type(other) is int and 0 < abs(pos - other) <= 2
                    and c.get("relative_position") == other - pos):
                return True
    return False


def _groups(scans, aliases):
    groups, seen, records = [], {}, {}
    rejected_context = 0
    bounded_text = False
    for scan_number, packet in enumerate(scans, 1):
        rows = packet.get("memories")
        if not isinstance(rows, list) or len(rows) > SCAN_LIMIT:
            raise ObjectRecallError("unbounded_scan")
        for rank, parent in enumerate(rows, 1):
            window = [parent]
            contexts = parent.get("conversation_context", [])
            if not isinstance(contexts, list) or len(contexts) > MAX_CONTEXT:
                raise ObjectRecallError("unbounded_context")
            for child in contexts:
                if _adjacent(parent, child):
                    duplicate = next((r for r in window if _identity(r) == _identity(child)), None)
                    if duplicate is None:
                        window.append(child)
                    elif (selection._metadata(duplicate), _text(duplicate), duplicate.get("content_truncated", False)) != (
                            selection._metadata(child), _text(child), child.get("content_truncated", False)):
                        raise ObjectRecallError("source_changed_within_window")
                else:
                    rejected_context += 1
            for row in window:
                identity, content = _identity(row), _text(row)
                signature = (selection._metadata(row), content, row.get("content_truncated", False))
                if identity in seen and seen[identity] != signature:
                    raise ObjectRecallError("source_changed_between_scans")
                seen[identity] = signature
                records[identity] = row
                bounded_text |= len(content) > MAX_SCAN_CHARS or row.get("content_truncated") is True
            labels = list(dict.fromkeys(label for row in window
                for label in _seed_labels(_text(row)[:MAX_SCAN_CHARS], aliases)))
            if not labels:
                continue
            members = []
            for row in window:
                content = _text(row)[:MAX_SCAN_CHARS]
                matched = [label for label in labels if label in content]
                if not matched:
                    continue
                # Correction markers affect review priority, not factual truth.
                correction = row.get("role") == "user" and bool(_CORRECTION.search(content))
                members.append((_identity(row), correction, matched))
            if not members:
                continue
            members.sort(key=lambda x: (not x[1], x[0] != _identity(parent)))
            groups.append({"members": members, "labels": labels,
                "scan": scan_number, "rank": rank, "parent": _identity(parent),
                "has_correction": any(m[1] for m in members)})
    groups.sort(key=lambda g: (not g["has_correction"], -min(2, len(g["labels"])), g["rank"], g["scan"]))
    return groups, records, rejected_context, bounded_text


def _prepare(groups, records, *, query, coverage):
    # Keep an evidence window together. Never fit only the assertion while
    # dropping the nearby correction because the last slot ran out.
    chosen, traces = [], {}
    pool = {member[0] for group in groups for member in group["members"]}
    request = {"question": query, "candidates": []}
    local = {}
    skipped_groups = 0
    for group in groups:
        fresh = [m for m in group["members"] if m[0] not in chosen]
        if len(group["members"]) > MAX_GROUP_RECORDS or len(chosen) + len(fresh) > selection.MAX_CANDIDATES:
            skipped_groups += 1
            continue
        proposed, new_local = [], {}
        for identity, correction, labels in fresh:
            row = records[identity]
            content = _text(row)
            position = min(content.find(label) for label in labels if label in content)
            start = max(0, position - 100)
            text = content[start:start + selection.MAX_EXCERPT_CHARS]
            cid = "c" + str(len(chosen) + len(proposed) + 1)
            truncated = row.get("content_truncated") is True
            proposed.append({"candidate_id": cid, "source_kind": row["source_kind"], "role": row.get("role"),
                "text": text, "excerpt_incomplete": truncated or start > 0 or len(text) < len(content)})
            new_local[cid] = {**selection._metadata(row), "text": text, "excerpt_start": start,
                "observed_content_sha256": selection.digest(content), "source_truncated": truncated,
                "observed_content_chars": len(content), "retrieval_round": group["scan"],
                "retrieval_rank": group["rank"], "object_review_priority": "user_correction" if correction else "linked_label"}
        if len(selection.encoded({**request, "candidates": request["candidates"] + proposed})) > selection.MAX_INPUT_BYTES:
            skipped_groups += 1
            continue
        request["candidates"].extend(proposed)
        local.update(new_local)
        chosen.extend(m[0] for m in fresh)
        for identity, _, labels in group["members"]:
            trace = {"scan": group["scan"], "rank": group["rank"], "parent_record_id": group["parent"][2], "labels": labels}
            paths = traces.setdefault(identity, [])
            if trace not in paths:
                paths.append(trace)
    for row in local.values():
        row["object_traces"] = traces[_identity(row)]
    return {"request": request, "local": local, "candidate_count": len(pool),
        "omitted_candidates": len(pool) - len(chosen), "coverage_gap": coverage,
        "skipped_groups": skipped_groups}


def run_object_shadow(shadow, *, query, as_of, runner=None):
    """Prepare a new *local* slate; do not mutate or call the existing selector.

    Use only with a frozen archive and its fixed read-only identity. New real
    excerpts need their own transport authorization before any model trial.
    """
    result = {"schema_version": VERSION, "status": "disabled", "queries": [], "scans": [],
        "retrieval_calls": 0, "prepared": None, "needs_more": True,
        "limitations": ["object_identity_unresolved", "not_semantically_verified", "not_exhaustive",
                        "record_time_is_not_event_time", "source_titles_are_clues_not_confirmed_names"]}
    try:
        eligible = planner.eligibility(query, as_of)
        if eligible != "eligible":
            result["status"] = eligible
            return result
        plan = planner.validate_plan(shadow.get("plan"), query)
        if plan["status"] != "planned" or plan["intent"] != "comparison" or len(plan["topic_terms"]) != 1:
            result["status"] = "unsupported_plan_original_preserved"
            return result
        if runner is None:
            return result
        aliases = public_subject_aliases(plan["topic_terms"][0])
        for term in aliases[:MAX_SCANS]:
            result["queries"].append(term)
            result["retrieval_calls"] += 1
            packet = runner({"query": term, "limit": SCAN_LIMIT, "as_of": as_of})
            if (packet.get("temporal_scope") or {}).get("status", "not_requested") != "not_requested":
                raise ObjectRecallError("scan_changed_calendar_scope")
            result["scans"].append(copy.deepcopy(packet))
        groups, records, rejected, text_bound = _groups(result["scans"], aliases)
        gaps = [(p.get("coverage") or {}).get("coverage_gap") for p in result["scans"]]
        coverage = True if any(g is True for g in gaps) else False if all(g is False for g in gaps) else None
        result["prepared"] = _prepare(groups, records, query=query, coverage=coverage)
        result.update(status="prepared_local_only", rejected_context=rejected, bounded_text=text_bound,
            scan_limit_reached=any(len(p["memories"]) == SCAN_LIMIT for p in result["scans"]))
    except Exception:
        result.update(status="failed_original_preserved", prepared=None)
    return result
