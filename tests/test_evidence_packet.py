"""Lossless evidence packing and bounded group allocation, entirely offline."""
import copy
import json

import pytest

from backend.memory import caretaker_evaluation as evaluation
from backend.memory import evidence_packet as packet
from backend.memory import object_evidence_review as review
from scripts import object_correction_exam as exam


def fixture(index=2):
    case = exam.load_cases("all", gaps=True)[index]
    prepared = exam.prepare_case(case)
    gold = {row["id"]: row for row in case["records"]}
    draft = {"judgments": [{"candidate_id": cid, "relation": gold[row["record_id"]]["relation"],
        "quote": row["text"] if gold[row["record_id"]]["relation"] in ("answers", "uncertain") else ""}
        for cid, row in prepared["local"].items()], "sufficiency": case["sufficiency"],
        "gap": case["gap"], "gaps": list(case["gaps"])}
    return review.make_brief(draft, prepared), prepared, draft


@pytest.mark.parametrize("index", range(4))
def test_exact_roundtrip_retains_quotes_every_reference_and_order_link(index):
    plain, _, _ = fixture(index)
    before = copy.deepcopy(plain)
    wire = packet.pack(plain)
    saved = json.loads(json.dumps(wire, ensure_ascii=False))
    assert packet.expand(saved) == before and packet.pack(packet.expand(wire)) == wire
    assert plain == before and saved == wire
    assert wire["gap"] == plain["gap"] and wire["gaps"] == plain["gaps"]
    assert wire["needs_more_evidence"] == plain["needs_more_evidence"]


def test_catalogue_aliases_are_not_source_authority():
    plain, prepared, _ = fixture()
    sources = {row["record_id"]: {**row, "content": row["text"]} for row in prepared["local"].values()}
    wire = packet.pack(plain)
    assert evaluation.verify_brief(wire, sources.get)["all_references_valid"]
    assert evaluation.verify_brief(wire, sources.get) == evaluation.verify_brief(plain, sources.get)
    changed = packet.expand(wire)
    changed["items"][0]["references"][0]["conversation_id"] = "wrong-room"
    changed_wire = packet.pack(changed)
    checked = evaluation.verify_brief(changed_wire, sources.get)
    assert not checked["all_references_valid"]
    assert "source_identity_mismatch" in checked["checks"][0]["reasons"]


def test_duplicate_words_keep_distinct_sources_and_multiple_quotes_of_one_source():
    plain, _, _ = fixture(0)
    first = plain["items"][0]["references"][0]
    other = {**first, "record_id": "independent-source", "conversation_id": "independent-room"}
    plain["items"][0]["references"].extend([other, copy.deepcopy(first)])
    wire = packet.pack(plain)
    assert len(wire["reference_catalog"]["rows"]) == 2
    assert len(wire["items"][0]["references"]) == 3
    assert packet.expand(wire) == plain
    # Same source, different exact span is a different reference, never merged.
    another_item = copy.deepcopy(plain["items"][0])
    another_item["references"] = [{**first, "quote_start": first["quote_start"] + 1}]
    another_item["text"] = another_item["text"][1:]
    plain["items"].append(another_item)
    assert packet.expand(packet.pack(plain)) == plain


def test_same_record_id_cannot_stand_for_inconsistent_sources():
    plain, _, _ = fixture(0)
    ref = plain["items"][0]["references"][0]
    plain["items"][0]["references"].append({**ref, "conversation_id": "another-room"})
    with pytest.raises(packet.PacketError):
        packet.pack(plain)


@pytest.mark.parametrize("mutation", ["encoding", "missing_catalogue", "dangling_ref", "unused_row", "duplicate_column",
    "defaults_overlap", "wrong_row_width", "dictionary_index", "dictionary_boolean", "too_many_rows", "bad_hash"])
def test_malformed_catalogue_is_rejected_without_reading_any_source(mutation):
    plain, _, _ = fixture()
    wire = packet.pack(plain)
    catalog = wire["reference_catalog"]
    if mutation == "encoding":
        wire["reference_encoding"] = "unknown"
    elif mutation == "missing_catalogue":
        del wire["reference_catalog"]
    elif mutation == "dangling_ref":
        wire["items"][0]["references"] = ["s99"]
    elif mutation == "unused_row":
        catalog["rows"]["s12"] = list(catalog["rows"]["s1"])
    elif mutation == "duplicate_column":
        catalog["columns"].append(catalog["columns"][0])
    elif mutation == "defaults_overlap":
        catalog["defaults"][catalog["columns"][0]] = None
    elif mutation == "wrong_row_width":
        catalog["rows"]["s1"].append(None)
    elif mutation in ("dictionary_index", "dictionary_boolean"):
        column = "record_id"
        catalog["dictionaries"][column] = ["fictional"]
        catalog["rows"]["s1"][catalog["columns"].index(column)] = -1 if mutation == "dictionary_index" else True
    elif mutation == "too_many_rows":
        catalog["rows"] = {"s" + str(i): list(catalog["rows"]["s1"]) for i in range(1, 14)}
    else:
        catalog["rows"]["s1"][catalog["columns"].index("observed_content_sha256")] = "not-a-fingerprint"
    with pytest.raises(packet.PacketError):
        packet.expand(wire)
    def forbidden(_):
        pytest.fail("malformed packet must fail before a source read")
    check = evaluation.verify_brief(wire, forbidden)
    assert not check["all_references_valid"] and check["references_checked"] == 0


def test_missing_or_wrong_source_order_links_are_not_dropped_or_guessed():
    plain, _, _ = fixture(3)
    wire = packet.pack(plain)
    group = wire["source_order_context"][0]
    group["source_order_windows"][0]["reference_keys"] = ["s1", "s99"]
    with pytest.raises(packet.PacketError):
        packet.expand(wire)
    plain["source_order_context"][0]["for_record_ids"] = ["unknown-record"]
    with pytest.raises(packet.PacketError):
        packet.pack(plain)


def test_empty_packet_is_not_an_answer_and_legacy_packets_are_unchanged():
    plain, _, _ = fixture(0)
    plain["items"] = []
    wire = packet.pack(plain)
    assert packet.expand(wire) == plain and wire["needs_more_evidence"]
    legacy = {"items": [{"text": "旧本机短摘录", "record_id": "old"}]}
    assert packet.expand(legacy) == legacy


def crowded():
    rows = []
    for i in range(12):
        text = f"合成记录{i}：" + ("这段保存当前物品说明及对应关系。" * (8 if i % 2 == 0 else 14))
        rows.append({"record_id": f"synthetic-record-{i}-" + "a" * 64, "content": text,
            "source_kind": "synthetic-conversation-capture", "conversation_id": "synthetic-room-" + "b" * 56,
            "role": "user" if i % 2 else "assistant", "created_at": f"2032-01-01T00:00:{i:02d}Z"})
    prepared = review.selection.prepare({"original": {"memories": rows[::2], "coverage": {"coverage_gap": False}},
        "passes": [{"memories": rows[1::2]}]}, query="这些物品有什么不同？")
    prepared["request"]["source_order_windows"] = [
        {"candidate_ids": [f"c{i}", f"c{i + 1}"]} for i in range(1, 12, 2)]
    draft = {"judgments": [{"candidate_id": cid, "relation": "uncertain" if i % 2 == 0 else "context",
        "quote": row["text"] if i % 2 == 0 else ""} for i, (cid, row) in enumerate(prepared["local"].items())],
        "sufficiency": "ambiguous", "gap": "object_identity", "gaps": ["object_identity"]}
    return prepared, draft, {row["record_id"]: row for row in rows}


def test_budget_is_measured_after_packing_and_restores_previously_withheld_groups():
    prepared, draft, records = crowded()
    before = copy.deepcopy((prepared, draft))
    legacy = review.make_brief(draft, prepared)
    wire = review.make_brief(draft, prepared, compact=True)
    restored = packet.expand(wire)
    assert legacy["omitted_selected"] > 0
    assert len(restored["items"]) == 6 and restored["omitted_selected"] == 0
    assert len(review.selection.encoded(wire)) <= review.selection.MAX_BRIEF_BYTES
    assert len(review.selection.encoded(restored)) > review.selection.MAX_BRIEF_BYTES
    assert sum(len(group["items"]) for group in restored["source_order_context"]) == 6
    assert restored["needs_more_evidence"] and evaluation.verify_brief(wire, records.get)["all_references_valid"]
    assert (prepared, draft) == before


def test_tight_budget_still_withholds_whole_related_groups(monkeypatch):
    prepared, draft, _ = crowded()
    monkeypatch.setattr(review.selection, "MAX_BRIEF_BYTES", 5000)
    wire = review.make_brief(draft, prepared, compact=True)
    restored = packet.expand(wire)
    selected = {ref["record_id"] for item in restored["items"] for ref in item["references"]}
    companions = {ref["record_id"] for group in restored["source_order_context"] for item in group["items"] for ref in item["references"]}
    for i in range(1, 12, 2):
        assert (prepared["local"][f"c{i}"]["record_id"] in selected) == (prepared["local"][f"c{i+1}"]["record_id"] in companions)
    assert len(review.selection.encoded(wire)) <= 5000 and restored["omitted_selected"] > 0
    assert restored["needs_more_evidence"]


def test_packing_failure_preserves_original_fallback(monkeypatch):
    case = exam.load_cases("all", gaps=True)[0]
    _, _, draft = fixture(0)
    def invalid(_):
        raise packet.PacketError("invalid_compact_evidence_packet")
    monkeypatch.setattr(packet, "pack", invalid)
    fallback = {"items": [{"text": "原始安全目录"}]}
    result = review.refine_object_shadow(exam.build_scout(case), fallback_brief=fallback, reviewer=lambda _: draft)
    assert result["status"] == "failed_original_brief_preserved" and result["brief"] == fallback
