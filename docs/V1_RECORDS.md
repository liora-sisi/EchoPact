# Echo Pact record packages and offline recall

## Protocol

The original protocol identifier is `echo-pact-records-v1`. It remains fully
supported. A JSON package has this shape:

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

### Compact multi-branch records

`echo-pact-records-v2` is the source-neutral compact form for a message that
belongs to more than one branch. It stores content and provenance once and
uses ordered branch memberships instead of copying the complete record for
every root-to-leaf path:

```json
{
  "schema_version": "echo-pact-records-v2",
  "records": [
    {
      "record_id": "stable-source-message-id",
      "source_kind": "conversation_export",
      "source_ref": "synthetic://example/conversation#message-1",
      "conversation_id": "conversation-1",
      "branch_memberships": [
        {"branch_id": "alternate", "position": 0},
        {"branch_id": "main", "position": 0}
      ],
      "message_id": "message-1",
      "role": "user",
      "content": "shared record text",
      "created_at": "2026-08-01T10:00:00Z",
      "verified": true,
      "authority": "user-confirmed",
      "source_cutoff_at": "2026-08-01T00:00:00Z",
      "conflict_group_id": null
    }
  ]
}
```

Each membership requires a stable `branch_id` and a zero-based `position`.
V2 record identity is the source message within its conversation, not one
branch occurrence. Reimporting the same content may add a previously unseen
branch membership, but it never duplicates or overwrites message content.
Conflicting content or conflicting positions fail preflight.
V2 JSON is serialized without presentation whitespace to keep large offline
packages smaller; this does not change its canonical field or array order.

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

Migration version 1 creates these objects:

- `schema_migrations`;
- `records_v1` and non-unique provenance indexes;
- `records_v1_fts` using local SQLite FTS5 with the trigram tokenizer;
- `records_v1_index_state` and synchronization triggers.

Migration version 2 adds only
`records_v1_branch_memberships` and its lookup index. Existing V1 rows receive
one compatibility membership with unknown position; no existing record or
content is rewritten. New V2 rows store one record/FTS document plus as many
small membership rows as their branch topology requires.

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

Recall runs entirely in SQLite. It does not use Chroma, the constant mock vector,
an API key, or a network request. Every result includes record content and
timestamp, source kind/reference, conversation/branch/message identifiers,
role, verification and authority, conflict group, source cutoff, confidence,
the compatibility `branch_id`, complete `branch_ids`/ordered memberships, and
the actual recall mode.

`confidence` is a deterministic evidence score, not a probability:

- local lexical match: +0.50;
- exact query substring: +0.20;
- verified source: +0.15;
- authoritative origin: +0.10;
- traceable `source_ref`: +0.05.

The response repeats this rule and the evidence used for each result.

### Projection-aware recall

`POST /api/v1/recall/projected` keeps the V1 evidence recall unchanged and
adds authenticated-agent active Claims, conflict adjudication annotations, and
an explicit projection freshness signal. A `cred_id.secret` Bearer credential
derives the agent identity; a request-body `agent_id`, when present during the
compatibility period, is only checked for equality and never selects identity.
Legacy `ACCESS_CODE` maps only to `agt-legacy`.

Adjudication is presentation-only: it never hides, reorders, or reweights an
evidence result and never automatically chooses a side from authority,
verification, confidence, or source count. `unprojected` records remain in the
result. A stale Claim is returned with a reason so callers can avoid trusting
its derived interpretation; recall itself never rebuilds or writes projection
state. Evidence visibility is applied before ranking and `LIMIT`, and coverage
is calculated from the same visible set. If any Claim or conflict-member
evidence becomes invisible, derived content and adjudication details are
replaced with a restricted placeholder.

## Knowledge coverage

`verified_knowledge_cutoff_at` is the latest cutoff declared by a verified,
non-`recent_patch` record. `latest_imported_record_at` is the newest record
timestamp, regardless of verification. An unverified `recent_patch` after the
cutoff can be returned but cannot move the verified cutoff.

When `as_of` is later than the verified cutoff, `coverage_status` is
`outside_verified_cutoff` and `coverage_gap` is true. Without `as_of`, temporal
coverage is deliberately reported as `not_assessed_without_as_of`; the system
does not guess a date from free-form query text.
