"""Isolated synthetic prototype: precompute drafts, review, retrieve, expand.

No production database, provider client, credential or network dependency. A
generator is injected only into ingest(); lookup()/expand() never generate text.
"""
from __future__ import annotations

import copy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .caretaker_summary import SummaryError, validate_records
from . import caretaker_summary_v4 as contract

APP_ID = 0x45505343
SCHEMA = "echo-summary-cabinet-synthetic-v1"
SOURCES = {"synthetic-gpt", "synthetic-codex", "synthetic-browser"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def _name(value):
    return isinstance(value, str) and 0 < len(value) <= 120 and value.strip() == value


def plan(corpus, *, model, provider="injected"):
    """Bounded room windows. A changed room gets a new revision, never silent reuse."""
    if not _name(provider) or not _name(model) or not isinstance(corpus, list) or not 1 <= len(corpus) <= 12:
        raise SummaryError("invalid_cabinet_corpus")
    recipe = digest({"schema": contract.SUMMARY_SCHEMA, "prompt": contract.SYSTEM_PROMPT,
                     "validator_revision": "v4b", "window": "six_records_8000_chars_overlap_one_v1"})
    jobs, identities = [], set()
    for group in corpus:
        if not isinstance(group, dict) or set(group) != {"scope_id", "source_namespace", "room_id", "records"}:
            raise SummaryError("invalid_cabinet_group")
        identity = tuple(group[k] for k in ("scope_id", "source_namespace", "room_id"))
        if (not all(_name(x) for x in identity) or group["source_namespace"] not in SOURCES
                or identity in identities):
            raise SummaryError("invalid_cabinet_identity")
        identities.add(identity)
        records = group["records"]
        if not isinstance(records, list) or not 1 <= len(records) <= 120:
            raise SummaryError("invalid_cabinet_record_count")
        ids = set()
        for record in records:
            validate_records([record])
            if record["record_id"] in ids:
                raise SummaryError("duplicate_cabinet_record_id")
            ids.add(record["record_id"])
        revision = digest(group)
        start = 0
        while start < len(records):
            end, chars = start, 0
            while end < len(records) and end - start < 6 and chars + len(records[end]["content"]) <= 8000:
                chars += len(records[end]["content"])
                end += 1
            window = copy.deepcopy(records[start:end])
            validate_records(window)
            spec = {"scope_id": identity[0], "source_namespace": identity[1], "room_id": identity[2],
                    "room_revision": revision, "start_record": start, "end_record": end,
                    "source_sha256": digest(window), "provider": provider,
                    "model": model, "recipe_sha256": recipe}
            jobs.append({"job_id": digest(spec), "spec": spec, "records": window})
            if len(jobs) > 120:
                raise SummaryError("cabinet_plan_too_large")
            if end == len(records):
                break
            start = max(start + 1, end - 1)
    return jobs


class Cabinet:
    def __init__(self, path, *, read_only=False):
        self.path = Path(path)
        self.read_only = read_only
        # Create exclusively or inspect an existing cabinet read-only before any
        # SQLite write. Never initialize/migrate an unrelated or original DB.
        try:
            if read_only:
                if not self.path.is_file():
                    raise SummaryError("summary_cabinet_missing")
                raise FileExistsError
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            try:
                with closing(sqlite3.connect(uri, uri=True)) as check:
                    if (check.execute("PRAGMA application_id").fetchone()[0] != APP_ID
                            or check.execute("SELECT value FROM meta WHERE key='schema'").fetchone() != (SCHEMA,)):
                        raise SummaryError("not_a_summary_cabinet")
            except sqlite3.Error:
                raise SummaryError("not_a_summary_cabinet") from None
            new = False
        else:
            os.close(fd)
            new = True
        self.db = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
                   if read_only else sqlite3.connect(self.path))
        self.db.row_factory = sqlite3.Row
        if read_only:
            self.db.execute("PRAGMA query_only=ON")
        if new:
            with self.db:
                self.db.execute(f"PRAGMA application_id={APP_ID}")
                self.db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                self.db.execute("INSERT INTO meta VALUES('schema',?)", (SCHEMA,))
                self.db.execute("""CREATE TABLE jobs(
                    job_id TEXT PRIMARY KEY, spec_json TEXT NOT NULL, state TEXT NOT NULL,
                    result_json TEXT, error_code TEXT, created_at TEXT NOT NULL)""")
                self.db.execute("""CREATE TABLE reviews(
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    accepted INTEGER NOT NULL, result_sha256 TEXT NOT NULL,
                    reviewer TEXT NOT NULL, note TEXT NOT NULL, at TEXT NOT NULL)""")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def state(self, job_id):
        row = self.db.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row[0] if row else None

    def ingest(self, jobs, generate):
        if self.read_only:
            raise SummaryError("summary_cabinet_read_only")
        outcomes = []
        for job in jobs:
            self._check_job(job)
            # Durable intent precedes both the budget reservation and API call.
            # Interrupted/failed jobs stay visible; resume NEVER retries them.
            with self.db:
                inserted = self.db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,NULL,NULL,?)",
                    (job["job_id"], encoded(job["spec"]), "started", _now())).rowcount
            if not inserted:
                outcomes.append({"job_id": job["job_id"], "state": self.state(job["job_id"]), "reused": True})
                continue
            result, raw_output, error, state = None, None, None, "draft"
            try:
                result = generate(copy.deepcopy(job["records"]), job_id=job["job_id"])
                if not isinstance(result, dict) or result.get("model") != job["spec"]["model"]:
                    raise SummaryError("cabinet_generator_identity_mismatch")
                raw_output = result["model_summary"]
                packet = contract.validate_summary(raw_output, job["records"])
                # Keep only the final model draft. Spending stays in its own ledger.
                result = {"model": result["model"], "model_summary": packet["model_summary"]}
                if packet["summary"]["status"] != "ok":
                    state = "rejected"
                    error = "summary_not_ok"
            except SummaryError as exc:
                rejected = getattr(exc, "rejected_draft", None)
                if rejected is None and raw_output is not None:
                    rejected = {"accepted": False, "final_content": raw_output}
                result = {"rejected_draft": rejected} if rejected is not None else None
                error = str(exc) if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,120}", str(exc)) else "generation_failed"
                state = "rejected" if result else "failed"
            except Exception:
                result, error, state = None, "generation_failed", "failed"
            with self.db:
                self.db.execute("UPDATE jobs SET state=?,result_json=?,error_code=? WHERE job_id=?",
                    (state, encoded(result) if result is not None else None, error, job["job_id"]))
            outcomes.append({"job_id": job["job_id"], "state": state, "error_code": error, "reused": False})
            if state == "failed":
                break  # Transport/auth/incomplete failure: no implicit retry or extra paid cells.
        return outcomes

    @staticmethod
    def _check_job(job):
        if (not isinstance(job, dict) or set(job) != {"job_id", "spec", "records"}
                or digest(job["spec"]) != job["job_id"]
                or digest(job["records"]) != job["spec"].get("source_sha256")):
            raise SummaryError("invalid_cabinet_job")
        validate_records(job["records"])

    def _packet(self, job):
        self._check_job(job)
        row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        if not row or row["state"] != "draft" or row["spec_json"] != encoded(job["spec"]):
            raise SummaryError("cabinet_draft_unavailable")
        result = json.loads(row["result_json"])
        if result.get("model") != job["spec"]["model"]:
            raise SummaryError("cabinet_generator_identity_mismatch")
        packet = contract.validate_summary(result["model_summary"], job["records"])
        if packet["summary"]["status"] != "ok":
            raise SummaryError("summary_not_ok")
        return packet

    def review(self, job, *, accepted, reviewer, note):
        if self.read_only:
            raise SummaryError("summary_cabinet_read_only")
        if type(accepted) is not bool or not _name(reviewer) or not isinstance(note, str) or not 1 <= len(note) <= 1000:
            raise SummaryError("invalid_cabinet_review")
        # Neither a generator's accepted=true nor a schema pass is approval.
        self._packet(job)
        raw = self.db.execute("SELECT result_json FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0]
        with self.db:
            self.db.execute("INSERT INTO reviews(job_id,accepted,result_sha256,reviewer,note,at) VALUES(?,?,?,?,?,?)",
                (job["job_id"], int(accepted), hashlib.sha256(raw.encode()).hexdigest(), reviewer, note, _now()))

    def _approved(self, job_id):
        row = self.db.execute("""SELECT r.accepted,r.result_sha256,j.result_json FROM reviews r
            JOIN jobs j ON j.job_id=r.job_id WHERE r.job_id=? ORDER BY r.review_id DESC LIMIT 1""",
            (job_id,)).fetchone()
        return bool(row and row[0] and row[2] and row[1] == hashlib.sha256(row[2].encode()).hexdigest())

    def lookup(self, corpus, *, summary_model, scope_id, source_namespaces, terms, max_chars=3200,
               summary_provider="injected"):
        jobs = plan(corpus, model=summary_model, provider=summary_provider)
        if (not _name(scope_id) or not isinstance(source_namespaces, list) or not source_namespaces
                or any(s not in SOURCES for s in source_namespaces)
                or not isinstance(terms, list) or not 1 <= len(terms) <= 6
                or any(not isinstance(t, str) or not t.strip() or len(t) > 80 for t in terms)
                or type(max_chars) is not int or not 800 <= max_chars <= 8000):
            raise SummaryError("invalid_cabinet_query")
        terms = [t.casefold() for t in terms]
        result = {"schema": SCHEMA, "route": "summary", "query_kind": "literal_terms_prototype",
                  "items": [], "review_required": True, "model_calls": 0, "notes": []}
        eligible = [j for j in jobs if j["spec"]["scope_id"] == scope_id
                    and j["spec"]["source_namespace"] in source_namespaces]
        for job in eligible:
            if not self._approved(job["job_id"]):
                continue
            try:
                summary = self._packet(job)["summary"]
            except (SummaryError, ValueError, KeyError, TypeError):
                if "invalid_reviewed_card_excluded" not in result["notes"]:
                    result["notes"].append("invalid_reviewed_card_excluded")
                continue
            brief = [{k: f[k] for k in ("text", "event_status", "statement_role", "event_date", "planned_date")}
                     | {"previous_claim": f["supersedes"]["text"] if f["supersedes"] else None}
                     for f in summary["facts"]]
            searchable = "\n".join(str(f[k]) for f in brief
                for k in ("text", "previous_claim", "event_date", "planned_date") if f[k] is not None).casefold()
            if not any(term in searchable for term in terms):
                continue
            record_ids = list(dict.fromkeys(c["record_id"] for fact in summary["facts"]
                for c in fact["evidence"] + (fact["supersedes"]["evidence"] if fact["supersedes"] else [])))
            item = {"job_id": job["job_id"], "source_namespace": job["spec"]["source_namespace"],
                    "room_id": job["spec"]["room_id"], "facts": brief,
                    "uncertainties": summary["uncertainties"], "original_record_ids": record_ids}
            if len(encoded({**result, "items": result["items"] + [item]})) <= max_chars:
                result["items"].append(item)
            if len(result["items"]) == 3:
                break
        if result["items"]:
            result["notes"] = list(dict.fromkeys(result["notes"]))
            # A later invalid card can add a diagnostic after an earlier card fit.
            while result["items"] and len(encoded(result)) > max_chars:
                result["items"].pop()
        if result["items"]:
            return result
        result.update(route="original_fallback", notes=["no_usable_summary_not_evidence_of_absence"])
        seen = set()
        for job in eligible:
            for record in job["records"]:
                identity = (job["spec"]["source_namespace"], job["spec"]["room_id"], record["record_id"])
                if identity in seen or not any(t in record["content"].casefold() for t in terms):
                    continue
                seen.add(identity)
                item = {"source_namespace": identity[0], "room_id": identity[1], "record_id": identity[2],
                        "excerpt": record["content"][:240], "excerpt_is_prefix": True,
                        "original_length": len(record["content"]), "truncated": len(record["content"]) > 240}
                if len(encoded({**result, "items": result["items"] + [item]})) <= max_chars:
                    result["items"].append(item)
                if len(result["items"]) == 3:
                    return result
        return result

    def expand(self, corpus, *, summary_model, scope_id, source_namespaces, job_id,
               summary_provider="injected"):
        # Resolve only against CURRENT source material in the requested scope.
        for job in plan(corpus, model=summary_model, provider=summary_provider):
            if (job["job_id"] == job_id and job["spec"]["scope_id"] == scope_id
                    and job["spec"]["source_namespace"] in source_namespaces and self._approved(job_id)):
                packet = self._packet(job)
                cited = {c["record_id"] for fact in packet["summary"]["facts"]
                         for c in fact["evidence"] + (fact["supersedes"]["evidence"] if fact["supersedes"] else [])}
                return {"source_namespace": job["spec"]["source_namespace"], "room_id": job["spec"]["room_id"],
                        "records": [copy.deepcopy(r) for r in job["records"] if r["record_id"] in cited],
                        "source_sha256": job["spec"]["source_sha256"], "model_calls": 0}
        raise SummaryError("cabinet_card_stale_unreviewed_or_not_visible")


def _now():
    return datetime.now(timezone.utc).isoformat()
