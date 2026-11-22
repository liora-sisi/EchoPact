"""Lossless, local packing of verified references. Quotes are never rewritten.

One column catalogue stores repeated metadata once; source-order links use
packet-local keys. Expansion restores the exact original evidence structure.
This codec does not verify truth, resolve records, query a DB or invoke a model.
"""
from __future__ import annotations

import copy
import re

from .evidence_selector import encoded

ENCODING = "columnar_references_v1"
MAX_REFERENCES = 12
MAX_EXPANDED_BYTES = 128_000
FIELDS = ("record_id", "source_kind", "conversation_id", "role", "created_at", "conflict_group_id",
          "observed_content_sha256", "source_truncated", "quote_start", "quote_end", "quote_resolution")
SOURCE_FIELDS = FIELDS[:8]


class PacketError(ValueError):
    pass


def _require(ok):
    if not ok:
        raise PacketError("invalid_compact_evidence_packet")


def _reference(ref):
    _require(isinstance(ref, dict) and set(ref) == set(FIELDS))
    for field in FIELDS[:6]:
        _require(ref[field] is None or isinstance(ref[field], str) and len(ref[field]) <= 256)
    _require(bool(ref["record_id"]) and bool(ref["source_kind"]))
    _require(isinstance(ref["observed_content_sha256"], str)
             and re.fullmatch(r"[0-9a-f]{64}", ref["observed_content_sha256"]) is not None)
    _require(type(ref["source_truncated"]) is bool)
    _require(type(ref["quote_start"]) is int and type(ref["quote_end"]) is int
             and 0 <= ref["quote_start"] < ref["quote_end"] <= 2**63 - 1)
    _require(isinstance(ref["quote_resolution"], str) and 1 <= len(ref["quote_resolution"]) <= 80)


def _items(packet):
    items, bundles = packet.get("items"), packet.get("source_order_context", [])
    _require(isinstance(items, list) and len(items) <= 6 and isinstance(bundles, list) and len(bundles) <= 6)
    output = list(items)
    for bundle in bundles:
        _require(isinstance(bundle, dict) and isinstance(bundle.get("items"), list))
        output.extend(bundle["items"])
    _require(len(output) <= 12)
    for item in output:
        _require(isinstance(item, dict) and set(item) == {"text", "relation", "references"})
        _require(isinstance(item["text"], str) and 1 <= len(item["text"]) <= 400)
        _require(item["relation"] in ("answers", "uncertain", "context"))
        _require(isinstance(item["references"], list) and 1 <= len(item["references"]) <= MAX_REFERENCES)
    return output


def _record_keys(table):
    keys, identities = {}, {}
    for key, ref in table.items():
        rid = ref["record_id"]
        identity = tuple(ref[field] for field in SOURCE_FIELDS)
        _require(rid not in identities or identities[rid] == identity)
        identities[rid] = identity
        keys.setdefault(rid, key)
    return keys


def _check_order(packet):
    selected = {ref["record_id"] for item in packet["items"] for ref in item["references"]}
    windows_count = 0
    for bundle in packet.get("source_order_context", []):
        _require(set(bundle) == {"for_record_ids", "basis", "source_order_windows", "items"})
        _require(bundle["basis"] == "verified_source_order_windows_not_semantic_truth")
        ids = bundle["for_record_ids"]
        _require(isinstance(ids, list) and 1 <= len(ids) <= 12 and all(isinstance(rid, str) for rid in ids))
        _require(len(set(ids)) == len(ids) and set(ids) <= selected)
        members = set(ids) | {ref["record_id"] for item in bundle["items"] for ref in item["references"]}
        windows = bundle["source_order_windows"]
        _require(isinstance(windows, list))
        windows_count += len(windows)
        for window in windows:
            _require(isinstance(window, dict) and set(window) == {"record_ids"})
            order = window["record_ids"]
            _require(isinstance(order, list) and 2 <= len(order) <= 5
                     and all(isinstance(rid, str) for rid in order))
            _require(len(set(order)) == len(order) and set(order) <= members)
    _require(windows_count <= 24)


def pack(packet):
    """Use only exact equality to share metadata; preserve every text and link."""
    _require(isinstance(packet, dict) and not {"reference_encoding", "reference_catalog"} & set(packet))
    _require(len(encoded(packet)) <= MAX_EXPANDED_BYTES)
    result = copy.deepcopy(packet)
    items = _items(result)
    for item in items:
        for ref in item["references"]:
            _reference(ref)
    _check_order(result)
    table, lookup = {}, {}
    for item in items:
        keys = []
        for ref in item["references"]:
            identity = tuple(ref[field] for field in FIELDS)
            if identity not in lookup:
                key = "s" + str(len(table) + 1)
                lookup[identity] = key
                table[key] = {field: ref[field] for field in FIELDS}
            keys.append(lookup[identity])
        item["references"] = keys
    _require(len(table) <= MAX_REFERENCES)
    by_record = _record_keys(table)
    defaults = {field: next(iter(table.values()))[field] for field in FIELDS
                if table and all(ref[field] == next(iter(table.values()))[field] for ref in table.values())}
    columns = [field for field in FIELDS if field not in defaults] if table else []
    rows = {key: [ref[field] for field in columns] for key, ref in table.items()}
    dictionaries = {}
    for index, field in enumerate(columns):
        values = [row[index] for row in rows.values()]
        if not all(isinstance(value, str) for value in values):
            continue
        unique = list(dict.fromkeys(values))
        indices = [unique.index(value) for value in values]
        if len(encoded({field: unique})) + len(encoded(indices)) < len(encoded(values)):
            dictionaries[field] = unique
            for row, value in zip(rows.values(), indices):
                row[index] = value
    result["reference_encoding"] = ENCODING
    result["reference_catalog"] = {"defaults": defaults, "columns": columns,
        "dictionaries": dictionaries, "rows": rows}
    for bundle in result.get("source_order_context", []):
        bundle["for_reference_keys"] = [by_record[rid] for rid in bundle.pop("for_record_ids")]
        bundle["source_order_windows"] = [{"reference_keys": [by_record[rid] for rid in window["record_ids"]]}
                                           for window in bundle["source_order_windows"]]
    return result


def expand(packet):
    """Decode local keys, rejecting dangling/unused sources and ambiguous IDs.

Legacy packets are copied unchanged. Decoding is not source verification; the
caller must still resolve exact IDs/roles/times/spans with its read-only identity.
"""
    _require(isinstance(packet, dict) and len(encoded(packet)) <= MAX_EXPANDED_BYTES)
    if "reference_encoding" not in packet:
        _require("reference_catalog" not in packet)
        return copy.deepcopy(packet)
    _require(packet["reference_encoding"] == ENCODING)
    result = copy.deepcopy(packet)
    result.pop("reference_encoding")
    catalog = result.pop("reference_catalog", None)
    _require(isinstance(catalog, dict) and set(catalog) == {"defaults", "columns", "dictionaries", "rows"})
    defaults, columns, dictionaries, rows = (catalog[k] for k in ("defaults", "columns", "dictionaries", "rows"))
    _require(isinstance(defaults, dict) and isinstance(columns, list)
             and all(isinstance(c, str) for c in columns) and len(set(columns)) == len(columns)
             and isinstance(dictionaries, dict) and isinstance(rows, dict) and len(rows) <= MAX_REFERENCES)
    _require(not set(defaults) & set(columns) and set(defaults) | set(columns) == (set(FIELDS) if rows else set()))
    _require(set(dictionaries) <= set(columns))
    for values in dictionaries.values():
        _require(isinstance(values, list) and 1 <= len(values) <= MAX_REFERENCES
                 and all(isinstance(value, str) and len(value) <= 256 for value in values))
        _require(len(set(values)) == len(values))
    table = {}
    for key, values in rows.items():
        _require(isinstance(key, str) and re.fullmatch(r"s(?:[1-9]|1[0-2])", key) is not None
                 and isinstance(values, list) and len(values) == len(columns))
        ref = copy.deepcopy(defaults)
        for field, value in zip(columns, values):
            if field in dictionaries:
                _require(type(value) is int and 0 <= value < len(dictionaries[field]))
                value = dictionaries[field][value]
            ref[field] = value
        _reference(ref)
        table[key] = ref
    _record_keys(table)
    used = set()
    for item in _items(result):
        _require(all(isinstance(key, str) and key in table for key in item["references"]))
        used.update(item["references"])
        item["references"] = [copy.deepcopy(table[key]) for key in item["references"]]
    _require(used == set(table))

    def record_ids(keys):
        _require(isinstance(keys, list) and len(keys) <= MAX_REFERENCES
                 and all(isinstance(key, str) and key in table for key in keys))
        return [table[key]["record_id"] for key in keys]

    for bundle in result.get("source_order_context", []):
        _require(set(bundle) == {"for_reference_keys", "basis", "source_order_windows", "items"})
        bundle["for_record_ids"] = record_ids(bundle.pop("for_reference_keys"))
        _require(isinstance(bundle["source_order_windows"], list) and len(bundle["source_order_windows"]) <= 24)
        windows = []
        for window in bundle["source_order_windows"]:
            _require(isinstance(window, dict) and set(window) == {"reference_keys"})
            windows.append({"record_ids": record_ids(window["reference_keys"])})
        bundle["source_order_windows"] = windows
    _check_order(result)
    _require(len(encoded(result)) <= MAX_EXPANDED_BYTES)
    return result
