# V1-M2 development record

## Baseline

- Local branch: `v1-core-2026-08-08`
- Starting HEAD: `58ccb6adfbe0f2b909376334a4f75647c7cfe7fd`
- Starting worktree: clean
- Upstream: not configured
- Remote V1 branch matched starting HEAD
- Protected main, Fable, and P0 safety refs matched the work order

## Evidence gate

- Source archive: repository-external `preview.zip`
- Bytes: 248,532
- SHA-256: `889e513df805c2c7c9e2679ac8e9743d99103a910c6d436d6ea627aa95254e82`
- ZIP entries: 114
- Required settings, backup, model, and protocol-document files: present
- `.env`, credential, key, token, or real backup anomaly: none found
- Source archive and extracted source remain outside the Echo Pact repository

## Decisions

- Implement Ferry as `backend/adapters/room_ferry_v1.py`; do not modify M1
  records, database, importer, recall, or API contracts.
- Mirror Ferry's data-only JSON.stringify checksum and preserve input key/array
  order while hashing.
- Rebuild controlled message content from `contentParts`; do not trust arbitrary
  rendered HTML or silently discard a future type.
- Use original `createdAt` only. Import/export timestamps are audit or snapshot
  boundaries, never message-time fallbacks.
- Materialize root-to-leaf paths when a multi-branch tree is provable. Reject an
  incomplete tree rather than infer it from sequence or content.
- Follow Ferry's actual branch-count formula: zero is linear, while one means
  one extra child choice and therefore two recoverable paths.
- Keep all Ferry archives unverified and use `exportedAt` only as their
  unverified source cutoff.
- Make conversion output deterministic and atomically new-file-only.

All implementation and tests use synthetic data. No real Room Ferry backup,
real chat database, IndexedDB, Echo Pact live database, API, server, or VPS is
read or modified.

## Synthetic end-to-end evidence

The committed synthetic Ferry fixture passed dry-run with one conversation,
two messages, one deterministic branch path, a valid checksum, zero warnings,
zero fatal findings, and two estimated output records. Its input SHA-256 is
`5d84548434a1a9b9055d1ed818d393ef8f33ae24254e2f2ee5a52eecb205b583`.

Two independent conversions produced byte-identical 2,167-byte packages with
SHA-256 `d0daca1bfc1a96501c0ea797759a5fdcf6c5e71797a1e7ee82e9133f9a0cc29c`.
The first M1 import added two records; the repeat import added zero and skipped
two. Offline recall found the fixture-only `FERRY-DEMO-2042` fact through
`sqlite_fts5_trigram`, returned its Ferry source reference and derived branch,
kept `verified=false`, and reported `verified_cutoff_unknown` rather than
advancing trusted knowledge. The index consistency check reported two records,
two index-state rows, two FTS documents, and no missing, orphaned, or stale rows.

Final verification with bytecode and pytest cache disabled:

- adapter plus M1 targeted tests: `30 passed, 1 warning`;
- adapter and related import/recall tests: `64 passed, 1 warning`;
- full repository suite: `95 passed, 1 warning`.

The warning is the pre-existing Chroma telemetry deprecation warning for
`asyncio.iscoroutinefunction`; no V1-M2 test failed.
