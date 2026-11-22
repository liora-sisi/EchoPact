"""Isolated, bounded source adjacency for semantic windows; no API or writes.

Order comes from the frozen, principal-filtered canonical database, never from
retrieval ranking, timestamps, supplied conversation_context or model guesses.
The seed bridge remains independently usable and unchanged.
"""
from __future__ import annotations

import copy

from . import evidence_selector as selection
from . import evidence_packet as codec
from . import object_evidence_review as ordered
from .identity import visible_record_rowids_query
from .records_v1 import _connect_readonly
from .semantic_evidence import WindowBridge
from .semantic_shadow import SemanticError, canonical_hash

VERSION = "echo-semantic-ordered-evidence-shadow-v1"
RADIUS = 2
MAX_BRANCHES = 4
LIMITATIONS = ["bounded_adjacent_messages_only", "unseen_messages_may_change_interpretation",
              "source_order_is_not_event_time_or_truth"]


def _key(row):
    return row["record_id"], row["excerpt_start"], len(row["text"])


def _record_orders(windows, local, group):
    result = []
    for window in windows:
        if set(window["candidate_ids"]) <= group:
            ids = list(dict.fromkeys(local[cid]["record_id"] for cid in window["candidate_ids"]))
            value = {"record_ids": ids}
            if len(ids) > 1 and value not in result:
                result.append(value)
    return result


class OrderedWindowBridge:
    def __init__(self, db_path, agent_id, *, source_sha256):
        self.base = WindowBridge(db_path, agent_id, source_sha256=source_sha256)

    def check_snapshot(self):
        self.base.check_snapshot()

    def _neighborhood(self, conn, row, visibility, params):
        memberships = conn.execute("""SELECT bm.branch_id, bm.position FROM records_v1_branch_memberships bm
            JOIN records_v1 r ON r.id=bm.record_rowid WHERE r.record_id=?
            ORDER BY bm.branch_id, bm.position LIMIT ?""", (row["record_id"], MAX_BRANCHES + 1)).fetchall()
        if len(memberships) > MAX_BRANCHES:
            return None, "branch_limit"
        windows = []
        for membership in memberships:
            if type(membership["position"]) is not int:
                return None, "ambiguous_position"
            rows = conn.execute(f"""SELECT r.record_id, bm.position FROM records_v1 r
                JOIN records_v1_branch_memberships bm ON bm.record_rowid=r.id
                WHERE bm.branch_id=? AND bm.position BETWEEN ? AND ?
                  AND r.source_kind=? AND r.conversation_id=? AND r.id IN ({visibility})
                ORDER BY bm.position, r.record_id LIMIT 6""",
                [membership["branch_id"], max(0, membership["position"] - RADIUS), membership["position"] + RADIUS,
                 row["source_kind"], row["conversation_id"], *params]).fetchall()
            if len(rows) > 5 or len({r["position"] for r in rows}) != len(rows):
                return None, "ambiguous_position"
            if row["record_id"] not in {r["record_id"] for r in rows}:
                raise SemanticError("adjacent_anchor_not_visible")
            ids = [r["record_id"] for r in rows]
            if ids not in windows:
                windows.append(ids)
        return windows or [[row["record_id"]]], None if memberships else "order_unknown"

    def _assemble(self, seed):
        self.base.verify(seed)
        by_record = {}
        for row in seed["local"].values():
            by_record.setdefault(row["record_id"], []).append(copy.deepcopy(row))
        for rows in by_record.values():
            rows.sort(key=lambda row: row["excerpt_start"])
        anchors = sorted(by_record, key=lambda rid: (by_record[rid][0]["retrieval_rank"], rid))
        request = {"question": seed["request"]["question"], "candidates": [], "source_order_windows": []}
        local, keys, pool = {}, {}, {_key(row) for row in seed["local"].values()}
        audit = {"anchor_groups": len(anchors), "admitted_groups": 0, "withheld_groups": 0,
                 "branch_limit_groups": 0, "ambiguous_position_groups": 0, "order_unknown_groups": 0,
                 "radius": RADIUS, "max_branches": MAX_BRANCHES}
        conn = _connect_readonly(str(self.base.path))
        try:
            conn.execute("BEGIN")
            visibility, params = visible_record_rowids_query(conn, self.base.agent_id)
            for rid in anchors:
                parent = by_record[rid][0]
                neighborhoods, issue = self._neighborhood(conn, parent, visibility, params)
                if issue:
                    audit[issue + "_groups"] += 1
                if neighborhoods is None:
                    audit["withheld_groups"] += 1
                    continue
                members = list(dict.fromkeys([rid, *(r for w in neighborhoods for r in w)]))
                for member in members:
                    if member not in by_record:
                        page = self.base.gateway.expand({"record_id": member, "length": selection.MAX_EXCERPT_CHARS})
                        if not page["content"]:
                            raise SemanticError("empty_adjacent_message")
                        by_record[member] = [{**selection._metadata(page["record"]), "text": page["content"],
                            "excerpt_start": 0, "observed_content_sha256": page["content_sha256"],
                            "source_truncated": False, "observed_content_chars": page["content_chars"],
                            "retrieval_rank": parent["retrieval_rank"], "window_origin": "adjacent",
                            "fingerprint_basis": "authoritative_full_record"}]
                rows = [row for member in members for row in by_record[member]]
                pool.update(_key(row) for row in rows)
                proposed, local_next, keys_next = copy.deepcopy(request), copy.deepcopy(local), dict(keys)
                for row in rows:
                    if _key(row) in keys_next:
                        continue
                    cid = "c" + str(len(local_next) + 1)
                    keys_next[_key(row)] = cid
                    local_next[cid] = row
                    proposed["candidates"].append({"candidate_id": cid, "source_kind": row["source_kind"],
                        "role": row["role"], "text": row["text"], "excerpt_incomplete": row["excerpt_start"] > 0
                        or row["excerpt_start"] + len(row["text"]) < row["observed_content_chars"]})
                orders = [[keys_next[_key(by_record[member][0])] for member in w] for w in neighborhoods]
                orders += [[keys_next[_key(row)] for row in by_record[member]] for member in members]
                for order in orders:
                    value = {"candidate_ids": order}
                    if len(order) > 1 and value not in proposed["source_order_windows"]:
                        proposed["source_order_windows"].append(value)
                if (len(local_next) > selection.MAX_CANDIDATES or len(proposed["source_order_windows"]) > ordered.MAX_WINDOWS
                        or len(selection.encoded(proposed)) > selection.MAX_INPUT_BYTES):
                    audit["withheld_groups"] += 1
                    continue
                request, local, keys = proposed, local_next, keys_next
                audit["admitted_groups"] += 1
        finally:
            conn.close()
        if audit["withheld_groups"]:
            # Eagerly expanding early hits must not evict a useful later seed.
            # Keep the exact original candidate set and defer model review.
            # A subsequent shortlist stage, not a larger cap or gold-aware
            # reranking, must decide which neighborhoods to spend space on.
            audit["discarded_partial_groups"] = audit["admitted_groups"]
            audit.update(admitted_groups=0, withheld_groups=len(anchors), deferred=True)
            request = {**copy.deepcopy(seed["request"]), "source_order_windows": []}
            local = copy.deepcopy(seed["local"])
        audit["incomplete_cards"] = sum(card["excerpt_incomplete"] for card in request["candidates"])
        audit["adjacent_cards"] = sum(row["window_origin"] == "adjacent" for row in local.values())
        omitted = seed["omitted_candidates"] + len(pool) - len(local)
        prepared = {"request": request, "local": local, "seed": seed, "adjacency": audit,
                    "candidate_count": seed["omitted_candidates"] + len(pool), "omitted_candidates": omitted,
                    "coverage_gap": seed["coverage_gap"], "retain_selected_order": True,
                    "object_scout_needs_more": bool(omitted or audit["withheld_groups"] or audit["order_unknown_groups"]
                                                   or audit["incomplete_cards"]),
                    "binding": {**seed["binding"], "version": VERSION}, "limits": seed["limits"]}
        if local:
            ordered.validate_request(request)
        self.check_snapshot()
        prepared["prepared_sha256"] = canonical_hash(prepared)
        return prepared

    def prepare(self, raw, *, query, topic_terms=()):
        return self._assemble(self.base.prepare(raw, query=query, topic_terms=topic_terms))

    def verify(self, prepared):
        # Reconstruct both ACL-filtered neighbors and source order. Rehashing a
        # forged order list is not proof of provenance.
        if self._assemble(prepared["seed"]) != prepared:
            raise SemanticError("ordered_window_preparation_changed")

    def preview(self, prepared):
        self.verify(prepared)
        local, windows = prepared["local"], prepared["request"]["source_order_windows"]
        components = [{cid} for cid in local]
        for window in windows:
            group = set(window["candidate_ids"])
            touching = [part for part in components if part & group]
            components = [part for part in components if not part & group] + [set.union(group, *touching)]
        rank = {cid: i for i, cid in enumerate(local)}
        components.sort(key=lambda group: min(rank[cid] for cid in group))
        brief = {"schema_version": VERSION + "-preview", "generation": "unreviewed_verbatim_windows",
                 "evidence_status": "not_assessed", "needs_more_evidence": True, "coverage_gap": prepared["coverage_gap"],
                 "items": [], "source_order_context": [], "omitted_from_preview": prepared["candidate_count"],
                 "adjacency": prepared["adjacency"], "limitations": ["not_an_answer", *LIMITATIONS]}
        kept = 0
        for group in components:
            cids = sorted(group, key=rank.get)
            context_count = sum(len(b["items"]) for b in brief["source_order_context"])
            roots = max(1, len(cids) - (ordered.MAX_ORDER_CONTEXT_ITEMS - context_count))
            if len(brief["items"]) + roots > selection.MAX_ITEMS:
                continue
            items = []
            for cid in cids:
                row = local[cid]
                ref = {k: row[k] for k in codec.SOURCE_FIELDS}
                ref.update(quote_start=row["excerpt_start"], quote_end=row["excerpt_start"] + len(row["text"]),
                           quote_resolution="verbatim_unreviewed_window")
                items.append({"text": row["text"], "relation": "context", "references": [ref]})
            proposed = copy.deepcopy(brief)
            proposed["items"].extend(items[:roots])
            orders = _record_orders(windows, local, group)
            if items[roots:] or orders:
                proposed["source_order_context"].append({
                    "for_record_ids": list(dict.fromkeys(local[cid]["record_id"] for cid in cids[:roots])),
                    "basis": "verified_source_order_windows_not_semantic_truth", "source_order_windows": orders,
                    "items": items[roots:]})
            proposed["omitted_from_preview"] = prepared["candidate_count"] - kept - len(group)
            if len(selection.encoded(codec.pack(proposed))) <= selection.MAX_BRIEF_BYTES:
                brief, kept = proposed, kept + len(group)
        return codec.pack(brief)

    def review(self, prepared, *, selector=None):
        result = {"schema_version": VERSION, "status": "unreviewed_windows", "selector_attempts": 0,
                  "production_accepted": False, "brief": self.preview(prepared)}
        if prepared["adjacency"].get("deferred") is True:
            result["status"] = "adjacency_deferred_unreviewed_windows"
            return result
        if selector is None or not prepared["local"]:
            return result
        result["selector_attempts"] = 1
        try:
            draft = selector(copy.deepcopy(prepared["request"]))
            packed = ordered.make_brief(draft, prepared, compact=True)
            brief = codec.expand(packed)
            brief["adjacency"] = prepared["adjacency"]
            brief["limitations"].extend(LIMITATIONS[:2])
            packed = codec.pack(brief)
            if len(selection.encoded(packed)) > selection.MAX_BRIEF_BYTES or codec.expand(packed) != brief:
                raise SemanticError("invalid_ordered_selection_packet")
            result.update(status="selected_requires_review", brief=packed, judgments=copy.deepcopy(draft))
        except Exception:
            result["status"] = "selector_failed_unreviewed_windows"
        self.verify(prepared)
        return result
