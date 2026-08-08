# V1-M1 development record

Baseline checked on 2026-08-08:

- source branch `safety-p0-2026-07-02` at
  `28a96a6701162a08b6c60d02880268c3b45ebbb9` was clean;
- the GitHub safety branch pointed to the same commit;
- local work branch `v1-core-2026-08-08` was created without upstream;
- project Python uses SQLite 3.50.4 and an in-memory FTS5 probe passed.

Confirmed pre-V1 gaps:

- `scripts/import_history.py` parsed no records and only simulated checkpoint
  behavior;
- SQLite had legacy create-if-missing tables but no version ledger;
- legacy recall used Chroma plus a fixed mock vector when real embedding was
  off, so it could not demonstrate meaningful offline relevance;
- legacy records did not preserve package, conversation, branch, message,
  authority, and source-cutoff provenance together.

V1-M1 decisions:

- keep legacy tables and `/api/recall` untouched for compatibility;
- add immutable `records_v1` storage rather than forcing V1 provenance into the
  legacy `memories` shape;
- use transaction-coupled SQLite FTS5 trigram indexing, which supports local
  Chinese/ASCII substring recall and avoids a second persistent store;
- validate and conflict-check before insert, then commit bounded batches so a
  stopped import can be retried idempotently;
- require caller-supplied `as_of` for temporal coverage judgments rather than
  pretending to infer dates from arbitrary query text;
- keep conflict records side by side and return their provided group without
  automatic adjudication.

All V1 verification uses only `tests/fixtures/echo_pact_records_v1.json` or
temporary synthetic derivatives. No real chat database, key, API, or network
call is part of the V1 path or tests.

Verification on 2026-08-08:

- V1/import targeted tests: `15 passed, 1 dependency warning`;
- V1 plus legacy memory/recall/vector safety tests: `45 passed, 1 dependency warning`;
- full suite after final code changes: `77 passed, 1 dependency warning`;
- CLI demo first import: 4 added, 0 skipped, 0 failed;
- identical second import: 0 added, 4 skipped, 0 failed;
- demo consistency: 4 source records, 4 index-state rows, 4 FTS documents;
- demo recall returned the synthetic `NOVA-9` recent patch with full provenance,
  preserved the verified cutoff at `2026-08-01T00:00:00Z`, and marked an
  `as_of=2026-08-06T00:00:00Z` query outside verified coverage;
- fixture SHA-256 was unchanged before and after the demo import.

The single warning is an existing Chroma dependency deprecation notice for
`asyncio.iscoroutinefunction`; it is outside the V1 SQLite path.
