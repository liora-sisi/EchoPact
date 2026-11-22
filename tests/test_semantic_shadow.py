import json
import sqlite3

import numpy as np
import pytest

from backend.memory import semantic_shadow as core
from backend.memory.identity import register_agent, set_agent_enabled
from backend.memory.records_v1 import import_record_package, COMPACT_RECORD_SCHEMA_VERSION


class FakeEncoder:
    """Wiring only. Scores below are never reported as real model quality."""
    fingerprint = "synthetic-encoder-v1"
    dimensions = 2
    max_tokens = 384

    def count_tokens(self, text):
        return len(text)

    def documents(self, texts):
        return np.array([[1, .1] if "烧烤" in t else [.1, 1] for t in texts], dtype="<f4")

    def query(self, text):
        return np.array([1, .1], dtype="<f4")


def corpus(tmp_path, *, long=False):
    db, package = tmp_path / "source.sqlite3", tmp_path / "source.json"
    for agent in ("owner", "outsider"):
        register_agent(agent, agent, actor="test", db_path=str(db))
    records = []
    for i, text in enumerate([("天气晴朗。" * 1000 if long else "") + "烧烤店吃了羊肉串。", "阳台种了薄荷。"]):
        records.append({"record_id": f"r{i}", "source_kind": "synthetic", "source_ref": "synthetic://vector",
            "conversation_id": "room", "message_id": f"m{i}", "role": "user", "content": text,
            "created_at": "2035-05-01T12:00:00Z", "verified": False, "authority": "synthetic",
            "source_cutoff_at": "2035-05-01T12:00:00Z", "branch_memberships": [{"branch_id": "main", "position": i}]})
    package.write_text(json.dumps({"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records}), encoding="utf-8")
    import_record_package(str(package), db_path=str(db), owner_agent_id="owner", actor="test")
    return db


@pytest.mark.parametrize("text", ["", "字", "a" * 2000, "早安。\n" * 700, "😄" * 900])
def test_chunks_cover_full_original_including_tail_and_obey_limits(text):
    spans = list(core.chunks(text, FakeEncoder()))
    covered = set()
    for start, end in spans:
        assert 0 <= start < end <= len(text)
        assert len(text[start:end]) <= 384
        covered.update(range(start, end))
    assert covered == set(range(len(text)))


def test_live_index_returns_tail_offsets_without_rewriting_source(tmp_path):
    db = corpus(tmp_path, long=True)
    before = core.file_digest(db)
    index = tmp_path / "index.sqlite3"
    meta = core.build_index(db, "owner", index, FakeEncoder())
    rows = core.semantic_candidates(db, "owner", index, FakeEncoder(), "撸串吃的啥", 1)
    assert meta["records"] == 2 and meta["chunks"] > 2
    assert rows[0]["record"]["record_id"] == "r0"
    assert any("烧烤" in rows[0]["record"]["content"][p["start"]:p["end"]] for p in rows[0]["spans"])
    assert core.file_digest(db) == before
    connection = sqlite3.connect(index)
    assert "content" not in [r[1] for r in connection.execute("pragma table_info(chunks)")]
    connection.close()


def test_index_never_overwrites_source_or_existing_index(tmp_path):
    db = corpus(tmp_path)
    with pytest.raises(core.SemanticError, match="destination"):
        core.build_index(db, "owner", db, FakeEncoder())
    index = tmp_path / "index.sqlite3"
    core.build_index(db, "owner", index, FakeEncoder())
    with pytest.raises(core.SemanticError, match="destination"):
        core.build_index(db, "owner", index, FakeEncoder())


def test_index_bound_to_principal_and_new_visibility_invalidates_it(tmp_path):
    db = corpus(tmp_path)
    index = tmp_path / "index.sqlite3"
    core.build_index(db, "owner", index, FakeEncoder())
    with pytest.raises(core.SemanticError, match="mismatch"):
        core.semantic_candidates(db, "outsider", index, FakeEncoder(), "撸串", 2)
    set_agent_enabled("outsider", False, actor="test", db_path=str(db))
    with pytest.raises(core.SemanticError, match="mismatch"):
        core.semantic_candidates(db, "owner", index, FakeEncoder(), "撸串", 2)


def test_changed_model_and_corrupt_chunk_refuse_to_rank(tmp_path):
    db = corpus(tmp_path)
    index = tmp_path / "index.sqlite3"
    core.build_index(db, "owner", index, FakeEncoder())
    different = FakeEncoder()
    different.fingerprint = "different"
    with pytest.raises(core.SemanticError, match="mismatch"):
        core.semantic_candidates(db, "owner", index, different, "撸串", 2)
    with sqlite3.connect(index) as c:
        c.execute("UPDATE chunks SET end=100000")
    with pytest.raises(core.SemanticError, match="reference"):
        core.semantic_candidates(db, "owner", index, FakeEncoder(), "撸串", 2)


@pytest.mark.parametrize("vectors", [[[float('nan'), 1]], [[0, 0]], [[1, 2, 3]]])
def test_invalid_vectors_rejected(vectors):
    with pytest.raises(core.SemanticError):
        core.normalized(vectors, 2)


@pytest.mark.parametrize("query", ["昨天撸串吃什么？", "谁给谁送薄荷？", "GPT房间烧烤", "第一次撸串", "薄荷原话", "没有烧烤"])
def test_constraints_never_call_vector_route(tmp_path, query, monkeypatch):
    db = corpus(tmp_path)
    runner = core.HybridGateway(db, "owner", tmp_path / "absent.sqlite3", FakeEncoder())
    monkeypatch.setattr(core, "semantic_candidates", lambda *a, **k: pytest.fail("must bypass vectors"))
    args = {"query": query, "limit": 3, "as_of": "2035-05-02T00:00:00Z"}
    original = runner.gateway.recall(args)
    actual = runner.recall(args)
    trace = actual.pop("semantic_shadow")
    assert trace["status"] == "constraints_original_only" and actual == original


def test_missing_index_preserves_original_without_claiming_no_memory(tmp_path):
    db = corpus(tmp_path)
    runner = core.HybridGateway(db, "owner", tmp_path / "missing", FakeEncoder())
    args = {"query": "撸串吃的啥", "limit": 2}
    original = runner.gateway.recall(args)
    result = runner.recall(args)
    trace = result.pop("semantic_shadow")
    assert trace["status"] == "index_unavailable_original_only" and result == original


def test_fusion_keeps_total_limit_and_separate_rank_scores(tmp_path):
    db = corpus(tmp_path)
    index = tmp_path / "index.sqlite3"
    core.build_index(db, "owner", index, FakeEncoder())
    runner = core.HybridGateway(db, "owner", index, FakeEncoder())
    result = runner.recall({"query": "撸串吃的啥", "limit": 1})
    assert len(result["memories"]) == 1 and result["memories"][0]["record_id"] == "r0"
    trace = result["semantic_shadow"]
    assert trace["status"] == "hybrid_candidates_only" and not trace["production_accepted"]
    assert trace["selected"]["r0"]["semantic_rank"] == 1
    assert trace["selected"]["r0"]["rrf_score"] != trace["selected"]["r0"]["semantic_score"]


def test_disabled_identity_cannot_use_cached_index(tmp_path):
    db = corpus(tmp_path)
    index = tmp_path / "index.sqlite3"
    core.build_index(db, "owner", index, FakeEncoder())
    runner = core.HybridGateway(db, "owner", index, FakeEncoder())
    set_agent_enabled("owner", False, actor="test", db_path=str(db))
    with pytest.raises(ValueError):
        runner.recall({"query": "撸串吃的啥", "limit": 2})


def test_snapshot_change_during_query_cannot_return_stale_permissions(tmp_path):
    db = corpus(tmp_path)
    index = tmp_path / "index.sqlite3"
    encoder = FakeEncoder()
    core.build_index(db, "owner", index, encoder)
    def query(_):
        set_agent_enabled("outsider", False, actor="test", db_path=str(db))
        return np.array([1, .1])
    encoder.query = query
    with pytest.raises(core.SemanticError, match="changed_during_query"):
        core.semantic_candidates(db, "owner", index, encoder, "撸串", 2)


def test_hybrid_fallback_rechecks_identity_after_mid_query_revocation(tmp_path):
    db = corpus(tmp_path)
    index = tmp_path / "index.sqlite3"
    encoder = FakeEncoder()
    core.build_index(db, "owner", index, encoder)
    runner = core.HybridGateway(db, "owner", index, encoder)
    def query(_):
        set_agent_enabled("owner", False, actor="test", db_path=str(db))
        return np.array([1, .1])
    encoder.query = query
    with pytest.raises(ValueError):
        runner.recall({"query": "撸串吃的啥", "limit": 2})


def test_trial_rejects_private_output_in_repository_before_reading_source():
    from scripts import semantic_shadow_trial as trial
    from backend.memory.assistant_baseline import BaselineError
    with pytest.raises(BaselineError, match="outside_repository"):
        trial.compare("not-read.sqlite3", [], "2035-05-02T00:00:00Z", FakeEncoder(), trial.FIXTURE.parent)
