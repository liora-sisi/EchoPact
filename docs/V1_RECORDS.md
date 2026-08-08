# Echo Pact V1 record package and offline recall

## Protocol

The protocol identifier is `echo-pact-records-v1`. A JSON package has this
shape:

```json
{
  "schema_version": "echo-pact-records-v1",
  "records": [
    {
      "record_id": "stable-source-defined-id",
      "source_kind": "conversation_export",
      "source_ref": "synthetic://example/conversation#message-1",
      "conversation_id": "conversation-1",
      "branch_id": "main",
      "message_id": "message-1",
      "role": "user",
      "content": "record text",
      "created_at": "2026-08-01T10:00:00Z",
      "verified": true,
      "authority": "user-confirmed",
      "source_cutoff_at": "2026-08-01T00:00:00Z",
      "conflict_group_id": null
    }
  ]
}
```

JSONL contains one complete record object per non-empty line. Each JSONL record
must carry `schema_version`. Timestamps must be timezone-aware ISO-8601 values.
`conflict_group_id` is optional and otherwise preserved as provided.

Identity is `record_id`, not `message_id`. The same message identifier may
legitimately exist in different conversations or branches. Import behavior is:

- same `record_id`, exact same content: skip;
- same `record_id`, different content: fail the package preflight;
- never overwrite an existing record silently;
- validate the complete package before creating or changing a database.

## Import and interruption recovery

Use an explicit database path, especially during evaluation:

```bash
python scripts/import_history.py PACKAGE.json --db PATH_TO_COPY.db --batch-size 100
```

The result reports `added`, `skipped`, `failed`, `knowledge_cutoff_at`, and
`latest_record_at`. Every batch is one SQLite transaction. A terminated batch
rolls back; a previously committed batch is found by `record_id` on retry.
Rerun the identical command to continue without duplicates or gaps. The input
package is only read and is never rewritten.

## Migration and index safety

Migration version 1 only creates these new objects:

- `schema_migrations`;
- `records_v1` and non-unique provenance indexes;
- `records_v1_fts` using local SQLite FTS5 with the trigram tokenizer;
- `records_v1_index_state` and synchronization triggers.

It does not remove or rewrite legacy tables or columns. Migration statements
run inside `BEGIN IMMEDIATE`; any error rolls the migration back. A record, its
FTS row, and durable index state are also inserted in one transaction.

Before applying this migration to a future real database:

1. stop writers and record the database path and file size;
2. run SQLite integrity checks on the source database;
3. create a byte-for-byte backup outside the live path and calculate SHA-256;
4. rehearse migration and recall against the backup copy;
5. migrate the live database only after the copy passes.

The recovery boundary is simple: migration failure rolls back automatically;
for an operational rollback, stop the service and restore the verified
pre-migration database copy. Do not delete V1 tables from a live database as a
rollback technique.

Derived-index checks and repair are repeatable:

```bash
python scripts/import_history.py --db PATH_TO_COPY.db --check-index
python scripts/import_history.py --db PATH_TO_COPY.db --repair-index
```

Repair rebuilds only derived FTS/index state from immutable `records_v1`
content in one transaction.

## Recall API

The legacy `POST /api/recall` remains available. V1 adds:

```http
POST /api/v1/recall
Content-Type: application/json

{"query":"ORCHID-731","limit":5,"as_of":"2026-08-01T00:00:00Z"}
```

V1 runs entirely in SQLite. It does not use Chroma, the constant mock vector,
an API key, or a network request. Every result includes record content and
timestamp, source kind/reference, conversation/branch/message identifiers,
role, verification and authority, conflict group, source cutoff, confidence,
and the actual recall mode.

`confidence` is a deterministic evidence score, not a probability:

- local lexical match: +0.50;
- exact query substring: +0.20;
- verified source: +0.15;
- authoritative origin: +0.10;
- traceable `source_ref`: +0.05.

The response repeats this rule and the evidence used for each result.

## Knowledge coverage

`verified_knowledge_cutoff_at` is the latest cutoff declared by a verified,
non-`recent_patch` record. `latest_imported_record_at` is the newest record
timestamp, regardless of verification. An unverified `recent_patch` after the
cutoff can be returned but cannot move the verified cutoff.

When `as_of` is later than the verified cutoff, `coverage_status` is
`outside_verified_cutoff` and `coverage_gap` is true. Without `as_of`, temporal
coverage is deliberately reported as `not_assessed_without_as_of`; the system
does not guess a date from free-form query text.

