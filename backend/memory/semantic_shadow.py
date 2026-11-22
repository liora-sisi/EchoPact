"""Opt-in, local-only records-v1 semantic sidecar. Not a production entrypoint.

The original gateway remains the baseline and fallback. The sidecar contains
vectors and offsets, never a replacement source of truth. A changed source
snapshot (including visibility events) invalidates it rather than hiding drift.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
from pathlib import Path

from .identity import visible_record_rowids_query
from .records_v1 import _connect_readonly, _row_to_recall_result
from .query_caretaker import assess_question
from .local_embedding_config import MODEL, REVISION, FILES, QUERY_PREFIX

VERSION = "echo-semantic-shadow-v1"
MAX_RECORDS = 5000
MAX_CHUNKS = 20000
MAX_CANDIDATES = 20
MAX_CHARS = 360
OVERLAP = 48
RRF_K = 60


class SemanticError(ValueError):
    pass


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class LocalEncoder:
    """Only accepts a prepared, hash-checked local model; no download fallback."""
    def __init__(self, directory):
        import os
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        directory = Path(directory).resolve(strict=True)
        manifest = json.loads((directory / "echo-model-manifest.json").read_text(encoding="utf-8"))
        if (manifest.get("model") != MODEL or manifest.get("revision") != REVISION
                or manifest.get("dimensions") != 512 or manifest.get("query_prefix") != QUERY_PREFIX
                or manifest.get("normalize_embeddings") is not True
                or set(manifest.get("files", {})) != set(FILES)):
            raise SemanticError("unsupported_model_manifest")
        for name, expected in manifest["files"].items():
            if file_digest(directory / name) != expected:
                raise SemanticError("model_file_changed")
        modules = json.loads((directory / "modules.json").read_text(encoding="utf-8"))
        if [m.get("type") for m in modules] != ["sentence_transformers.models.Transformer",
                                               "sentence_transformers.models.Pooling",
                                               "sentence_transformers.models.Normalize"]:
            raise SemanticError("unsupported_model_modules")
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        self.model = SentenceTransformer(str(directory), device="cpu", local_files_only=True,
                                        trust_remote_code=False, model_kwargs={"use_safetensors": True})
        self.manifest = manifest
        self.fingerprint = canonical_hash(manifest)
        self.dimensions = manifest["dimensions"]
        self.max_tokens = min(384, self.model.max_seq_length - 2)
        self.prefix = manifest["query_prefix"]

    def count_tokens(self, text):
        return len(self.model.tokenizer.encode(text, add_special_tokens=True, truncation=False))

    def documents(self, texts):
        if any(self.count_tokens(t) > self.max_tokens for t in texts):
            raise SemanticError("document_would_be_truncated")
        return self.model.encode(texts, normalize_embeddings=True, batch_size=16,
                                 show_progress_bar=False, convert_to_numpy=True)

    def query(self, text):
        text = self.prefix + text
        if self.count_tokens(text) > self.model.max_seq_length:
            raise SemanticError("query_would_be_truncated")
        return self.model.encode([text], normalize_embeddings=True, show_progress_bar=False,
                                 convert_to_numpy=True)[0]


def chunks(text, encoder):
    """Natural character boundaries with measured token limits and full coverage."""
    start = 0
    while start < len(text):
        end = min(len(text), start + MAX_CHARS)
        if end < len(text):
            boundary = max(text.rfind(mark, start + MAX_CHARS // 2, end)
                           for mark in ("\n", "。", "！", "？", ";", ". "))
            if boundary >= 0:
                end = boundary + 1
        while encoder.count_tokens(text[start:end]) > encoder.max_tokens:
            if end - start <= 1:
                raise SemanticError("single_character_exceeds_model_limit")
            end = start + (end - start) // 2
        yield start, end
        if end == len(text):
            break
        start = end - min(OVERLAP, (end - start) // 4)


def visible_records(db_path, agent_id):
    conn = _connect_readonly(str(db_path))
    try:
        conn.execute("BEGIN")
        sql, params = visible_record_rowids_query(conn, agent_id)
        rows = conn.execute(f"SELECT r.* FROM records_v1 r WHERE r.id IN ({sql}) ORDER BY r.id LIMIT ?",
                            [*params, MAX_RECORDS + 1]).fetchall()
        if len(rows) > MAX_RECORDS:
            raise SemanticError("shadow_corpus_too_large")
        result = {}
        for row in rows:
            memberships = [dict(m) for m in conn.execute(
                "SELECT branch_id, position FROM records_v1_branch_memberships WHERE record_rowid=? ORDER BY branch_id",
                (row["id"],))]
            value = dict(row)
            value["memberships"] = memberships
            result[row["record_id"]] = value
        return result
    finally:
        conn.close()


def normalized(values, dimensions):
    import numpy as np
    array = np.asarray(values, dtype="<f4")
    if array.ndim != 2 or array.shape[1] != dimensions or not np.isfinite(array).all():
        raise SemanticError("invalid_vectors")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if not np.isfinite(norms).all() or (norms <= 0).any():
        raise SemanticError("zero_vector")
    return array / norms


def build_index(db_path, agent_id, index_path, encoder):
    """New sidecar only; bounded complete visible corpus, no silent sampling."""
    source = Path(db_path).resolve(strict=True)
    target = Path(index_path).resolve()
    if source == target or target.exists():
        raise SemanticError("index_destination_exists")
    if Path(str(source) + "-wal").exists():
        raise SemanticError("frozen_snapshot_required")
    before = file_digest(source)
    rows = visible_records(source, agent_id)
    pieces = []
    for record_id, row in rows.items():
        for start, end in chunks(row["content"], encoder):
            pieces.append((record_id, start, end, row["content"][start:end]))
            if len(pieces) > MAX_CHUNKS:
                raise SemanticError("shadow_chunk_limit")
    vectors = normalized(encoder.documents([p[3] for p in pieces]), encoder.dimensions) if pieces else []
    if len(vectors) != len(pieces):
        raise SemanticError("vector_count_mismatch")
    if Path(str(source) + "-wal").exists() or file_digest(source) != before:
        raise SemanticError("source_changed_during_build")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the unique destination; a failed build remains clearly incomplete.
    with target.open("xb"):
        pass
    conn = sqlite3.connect(target)
    try:
        conn.executescript("CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);"
                           "CREATE TABLE chunks (id INTEGER PRIMARY KEY, record_id TEXT NOT NULL,"
                           "start INTEGER NOT NULL, end INTEGER NOT NULL, vector BLOB NOT NULL);")
        meta = {"version": VERSION, "source_sha256": before, "agent_id": agent_id,
                "encoder": encoder.fingerprint, "dimensions": encoder.dimensions,
                "chunker": {"max_chars": MAX_CHARS, "overlap": OVERLAP, "max_tokens": encoder.max_tokens},
                "records": len(rows), "chunks": len(pieces), "complete": True}
        conn.executemany("INSERT INTO chunks(record_id,start,end,vector) VALUES(?,?,?,?)",
                         [(p[0], p[1], p[2], v.tobytes()) for p, v in zip(pieces, vectors)])
        conn.executemany("INSERT INTO meta VALUES(?,?)", [(k, json.dumps(v)) for k, v in meta.items()])
        conn.commit()
    finally:
        conn.close()
    return meta


def semantic_candidates(db_path, agent_id, index_path, encoder, query, limit):
    import numpy as np
    if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
        raise SemanticError("invalid_candidate_limit")
    rows = visible_records(db_path, agent_id)  # Active identity and live grants first.
    index = sqlite3.connect(Path(index_path).resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    try:
        index.execute("PRAGMA query_only=ON")
        meta = {k: json.loads(v) for k, v in index.execute("SELECT name,value FROM meta")}
        if (meta.get("version") != VERSION or meta.get("complete") is not True
                or meta.get("agent_id") != agent_id or meta.get("encoder") != encoder.fingerprint
                or meta.get("dimensions") != encoder.dimensions
                or meta.get("records") != len(rows)
                or meta.get("chunker") != {"max_chars": MAX_CHARS, "overlap": OVERLAP, "max_tokens": encoder.max_tokens}
                or Path(str(Path(db_path).resolve()) + "-wal").exists()
                or meta.get("source_sha256") != file_digest(db_path)):
            raise SemanticError("index_snapshot_or_model_mismatch")
        pieces = index.execute("SELECT record_id,start,end,vector FROM chunks ORDER BY id LIMIT ?",
                               (MAX_CHUNKS + 1,)).fetchall()
        if len(pieces) != meta.get("chunks") or len(pieces) > MAX_CHUNKS:
            raise SemanticError("invalid_index_size")
        if not pieces:
            return []
        for rid, start, end, vector in pieces:
            if rid not in rows or not 0 <= start < end <= len(rows[rid]["content"]):
                raise SemanticError("invalid_chunk_reference")
        matrix = normalized([np.frombuffer(p[3], dtype="<f4") for p in pieces], encoder.dimensions)
        q = normalized([encoder.query(query)], encoder.dimensions)[0]
        scores = matrix @ q
        order = sorted(range(len(pieces)), key=lambda i: (-float(scores[i]), pieces[i][0], pieces[i][1]))
        selected = {}
        for i in order:
            rid, start, end, _ = pieces[i]
            if rid not in selected:
                if len(selected) >= limit:
                    continue
                selected[rid] = {"record": rows[rid], "semantic_score": float(scores[i]), "spans": []}
            if len(selected[rid]["spans"]) < 2:
                selected[rid]["spans"].append({"start": start, "end": end})
        if (Path(str(Path(db_path).resolve()) + "-wal").exists()
                or meta["source_sha256"] != file_digest(db_path)):
            raise SemanticError("source_changed_during_query")
        return list(selected.values())
    finally:
        index.close()


class HybridGateway:
    """Optional runner for local experiments; constrained queries remain original.

    All query scopes currently stay with the established gateway. We deliberately
    do not claim general semantic filtering or automatic source/date parsing.
    """
    def __init__(self, db_path, agent_id, index_path, encoder):
        from ..mcp.readonly_server import ReadonlyGateway
        self.db_path, self.agent_id = str(db_path), agent_id
        self.index_path, self.encoder = index_path, encoder
        self.gateway = ReadonlyGateway(self.db_path, agent_id)
        self.gateway.validate()

    def recall(self, arguments):
        if set(arguments) - {"query", "limit", "as_of"}:
            raise SemanticError("unsupported_shadow_arguments")
        result = self.gateway.recall(arguments)
        result = copy.deepcopy(result)
        query, limit = arguments["query"], arguments.get("limit", 5)
        baseline = copy.deepcopy(result["memories"])
        trace = {"version": VERSION, "enabled": True, "production_accepted": False,
                 "status": "lexical_only", "baseline_ids": [r["record_id"] for r in baseline]}
        result["semantic_shadow"] = trace
        assessment = assess_question(query, arguments.get("as_of"))
        if assessment["decision"] != "eligible":
            trace.update(status="constraints_original_only", reasons=assessment["reasons"])
            return result
        try:
            found = semantic_candidates(self.db_path, self.agent_id, self.index_path, self.encoder, query, limit)
        except (OSError, ValueError, sqlite3.Error, RuntimeError):
            # Recheck visibility instead of returning a cached pre-revocation baseline.
            result = copy.deepcopy(self.gateway.recall(arguments))
            trace["baseline_ids"] = [r["record_id"] for r in result["memories"]]
            trace["status"] = "index_unavailable_original_only"
            result["semantic_shadow"] = trace
            return result
        entries, scores, rankings = {}, {}, {}
        for rank, row in enumerate(baseline, 1):
            rid = row["record_id"]
            entries[rid] = row
            scores[rid] = 1 / (RRF_K + rank)
            rankings[rid] = {"lexical_rank": rank}
        for rank, item in enumerate(found, 1):
            source, spans = item["record"], item["spans"]
            rid = source["record_id"]
            if rid not in entries:
                entries[rid] = _row_to_recall_result(source, query, "local_semantic_shadow", source["memberships"])
            scores[rid] = scores.get(rid, 0) + 1 / (RRF_K + rank)
            rankings.setdefault(rid, {}).update(semantic_rank=rank, semantic_score=item["semantic_score"],
                                                content_sha256=source["content_sha256"], spans=spans)
        ids = sorted(scores, key=lambda rid: (-scores[rid], rankings[rid].get("lexical_rank", math.inf), rid))[:limit]
        result["memories"] = [entries[rid] for rid in ids]
        trace.update(status="hybrid_candidates_only", semantic_ids=[v["record"]["record_id"] for v in found],
                     candidate_limit=limit, fusion="rrf", rank_constant=RRF_K,
                     selected={rid: {**rankings[rid], "rrf_score": scores[rid]} for rid in ids},
                     warning="similarity_is_not_evidence_sufficiency")
        from ..mcp.readonly_server import _bounded_result
        return _bounded_result(result)
