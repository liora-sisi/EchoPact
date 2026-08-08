# Room Ferry full-backup v1 adapter

## Scope and architecture

Room Ferry is the first source adapter, not a core dependency. All Ferry format
names, checksum behavior, content-part interpretation, and version checks live
in `backend/adapters/room_ferry_v1.py`. The source-neutral M1 records, importer,
SQLite schema, FTS5 recall, and `/api/v1/recall` do not import or depend on Ferry
types.

The adapter accepts exactly one strict UTF-8 JSON file. It does not read
IndexedDB, scan directories, parse TXT/Markdown as a machine format, open ZIP
backups, use the network, or call an embedding API.

## Source evidence

Evidence package:

- file: `preview.zip`
- SHA-256: `889e513df805c2c7c9e2679ac8e9743d99103a910c6d436d6ea627aa95254e82`
- app version: `0.1.9`
- build: `0.1.9-reader-search-date`
- database schema: `1`

Evidence locations inside the verified source package:

- `src/features/settings/backup.ts:13`: backup format version 1;
- `src/features/settings/backup.ts:73`: checksum helper;
- `src/features/settings/backup.ts:74`: UTF-8 SHA-256 of `JSON.stringify(data)`;
- `src/features/settings/backup.ts:105`: pretty serialization of the whole backup;
- `src/features/settings/backup.ts:111`: parse begins with JSON decoding;
- `src/features/settings/backup.ts:116`: format check;
- `src/features/settings/backup.ts:119`: formatVersion check;
- `src/features/settings/backup.ts:120`: schema compatibility check;
- `src/features/settings/backup.ts:126`: required array and stable-ID validation;
- `src/features/settings/backup.ts:127`: checksum recomputation after structural checks;
- `src/features/settings/backup.ts:145`: restore uses one read/write transaction;
- `src/domain/models.ts:10`: complete controlled `ContentPart` union;
- `src/domain/models.ts:38`: Message model;
- `src/domain/models.ts:42`: optional original source message ID;
- `src/domain/models.ts:43`: parent source message relationship;
- `src/domain/models.ts:46`: optional original message timestamp;
- `src/domain/models.ts:52`: stable sortKey;
- `src/normalizer/chatgpt.ts:196`: source message time becomes `createdAt`;
- `src/normalizer/chatgpt.ts:213`: sortKey is depth, time, and mapping key;
- `src/normalizer/fingerprint.ts:1`: text NFC/newline/trailing-space normalization;
- `src/normalizer/fingerprint.ts:10`: fingerprint SHA-256 implementation.

`parseBackup` failure order is JSON, format, formatVersion, supported schema,
required header fields, required arrays/stable IDs, then checksum. Restore is a
separate user-confirmed transaction; any constraint/write failure rolls the
transaction back.

## Checksum and field order

The checksum covers only the complete `data` object, including unrecognized
data fields. It does not cover the outer header. The input checksum is:

```text
lowercase_hex(SHA-256(UTF-8(JSON.stringify(parsed_backup.data))))
```

`JSON.stringify` is compact for checksum purposes. Object property insertion
order and array order therefore participate in the checksum. The outer backup
file itself is pretty-printed with two spaces, but its whitespace does not enter
the checksum.

The adapter additionally requires the declared algorithm to be `SHA-256` and
schema version to be exactly 1, because other schemas have not been mapped.

## Time semantics

`Message.createdAt` is the original source message timestamp normalized by
Room Ferry to epoch milliseconds. It maps to records-v1 `created_at` in UTC.

`firstImportedAt`, `lastImportedAt`, and backup `exportedAt` never substitute
for a missing message timestamp. Any non-empty message without a valid
`createdAt` makes formal conversion ineligible. `exportedAt` is used only as the
unverified source snapshot boundary in `source_cutoff_at`; every converted
record remains `verified=false`, so it cannot advance Echo Pact's verified
knowledge cutoff.

## Identity and provenance

Room Ferry `Message.id` is a stable Ferry database identity. Optional
`sourceMessageId` is kept separate and is not relabeled as an official ChatGPT
ID. Every `source_ref` contains:

- the exact input file SHA-256;
- the Ferry conversation ID;
- the Ferry message ID.

Record IDs are deterministic SHA-256 identities over Ferry conversation ID and
Ferry message ID. Branch membership is stored separately, so adding a branch
does not change the message identity. `contentFingerprint` is not used as an
identity: Room Ferry computes it from normalized text, role, and part types for
change/merge detection, so it is content-derived and may change.

## Branch preservation

Room Ferry has no explicit `branchId`. It preserves `sourceMessageId`,
`parentSourceMessageId`, `branchCount`, and current leaf information.
`branchCount` is only a count, never treated as topology.

- Ferry computes `branchCount` as the number of additional child choices, but
  old imported rows can contain stale counts. The recoverable parent graph is
  authoritative; a disagreement is reported and never used to flatten paths.
- When source IDs are complete, the preserved parent graph takes precedence and
  may reveal real paths even when a stale `branchCount` says zero.
- When source IDs are unavailable and `branchCount <= 1`, messages require
  unique non-empty `sortKey` values and form one deterministic fallback branch.
- Graph recovery requires unique `sourceMessageId` values and an acyclic,
  reachable topology. Its reconstructed branch count is compared with, but is
  not overridden by, the declaration.
- A single omitted structural parent ID may anchor one root component without
  creating a message. Multiple unconnected missing-parent anchors are fatal.
- Each root-to-leaf path becomes one deterministic branch. Compact records-v2
  stores each message body once and attaches one `{branch_id, position}`
  membership per path, so shared ancestors do not duplicate content or FTS rows.
- A branch ID hashes the conversation identity and the choices made at actual
  divergence points. It does not guess from title, content similarity, or time.

Ambiguous or cyclic multi-branch mapping is fatal and produces no formal output.

## Content parts and non-message data

Recognized Ferry v1 parts are:

- `text`;
- `image-placeholder`;
- `audio-placeholder`;
- `audio-transcription`;
- `attachment-placeholder`;
- `unknown` with an explicit summary.

The adapter rebuilds record content from controlled parts. Ferry's explicit
`unknown` part is preserved with a warning. A part type outside this union is
fatal. Empty messages are reported and skipped; message text is never printed
in the default dry-run report.

`importBatches` contributes counts/audit context only. `handoffDrafts` is
counted and never converted. `appMeta` contributes schema metadata only.
Unknown outer or data fields are warned and never silently ignored.

## Commands

Dry-run never creates a formal package or touches a database:

```bash
python scripts/adapt_room_ferry.py BACKUP.json --dry-run
```

Formal conversion refuses an existing output path and atomically creates a new
compact records-v2 JSON only after a clean dry-run:

```bash
python scripts/adapt_room_ferry.py BACKUP.json --output RECORDS_V2.json
python scripts/import_history.py RECORDS_V2.json --db TEMPORARY_COPY.db
```

The same input produces byte-identical output. A fatal conversion removes its
own temporary output and leaves no half package.

## Safe refusal conditions

Formal conversion is refused for malformed/non-UTF-8 JSON, files over 500 MB,
wrong format/version/schema/checksum algorithm, checksum mismatch, invalid or
duplicate Ferry IDs, orphan messages, message-count mismatch, missing original
time, unsupported role, ambiguous single-branch order, ambiguous/cyclic branch
mapping, multiple unconnected missing-parent anchors, schema metadata conflict,
or a new content-part type.
