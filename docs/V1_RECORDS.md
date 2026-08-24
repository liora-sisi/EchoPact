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
the actual recall mode. When the matched record has ordered branch positions,
the result also includes a bounded `conversation_context` window (one record
before and up to two after) from one deterministic valid branch. Each context
record carries its own source, role, timestamp and branch position; it is
evidence context, not a generated answer.

Lexical recall is precision-first and deterministic. It tries the normalized
exact phrase before any relaxation, recognizes a spaced all-ASCII name through
its compact alias, removes a small fixed set of Chinese question scaffolding,
preserves first-person scope, repairs only documented narrow speech-to-text
aliases, and gives first/earliest questions a literal topic-and-time tier,
relaxes long Chinese text into overlapping trigrams only when focused terms do
not find a result, and finally scores one- or two-character terms with escaped
parameterized `LIKE`. The exact tier remains `sqlite_fts5_trigram`; focused,
relaxed and fallback modes are explicitly labelled with `_focused`, `_relaxed`
or `_fallback`. This query planning is offline lexical matching, not semantic
embedding, and it never changes source records or the FTS index.

Preference questions with an explicit subject bind that literal subject to a
fixed, source-neutral set of preference/choice/answer markers. Latest-event
questions with at least two explicit subjects require every surviving literal
subject in the same evidence row and then sort matching evidence newest first.
This prevents the legacy longest-clause heuristic from dropping the object of a
preference or one half of a composite event. Neither rule contains private
answers or domain-specific synonyms; nearby turns remain provenance-bearing
context rather than generated conclusions.
For a latest composite event, conversational wording such as "what did you say
then" does not start a database-wide original-wording trace: event identity
remains constrained by all literal subjects and the bounded branch context
supplies the nearby answer. Explicit original-wording behavior remains unchanged
for non-composite and source-forensic queries.

Earliest-event ordering is still retrieval, not adjudication. Quoted stories,
hypotheticals and later recollections may all contain the same literal words;
Echo Pact returns traceable candidates and nearby turns instead of silently
declaring one interpretation to be historical fact.

For an earliest question that explicitly describes a shared two-person event,
M6.1 can run one bounded rescue pass over records visible to the caller. It
evaluates only fixed windows from one conversation and one branch, requires a
literal event anchor plus explicit dyadic participation evidence, and rejects
future plans, hypotheticals, negation, fictional framing, companion-only
statements and incompatible event objects. Generic meal questions additionally
require nearby food or meal evidence, so a phrase such as "eat an e-cigarette"
does not become a shared meal. The category guard is deterministic and does not
contain a private expected answer.

A qualifying result reports `recall_mode=sqlite_shared_event_window` and adds
`event_evidence`, `event_participation_evidence`, `event_start_at`,
`event_end_at`, `earliest_support_status`, `earliest_support_reason`, and
`assistant_identity_status`. Response-level `event_recall` reports the fixed
candidate limit, candidates/windows inspected, qualifying count and whether
the search was truncated. This is an earliest candidate within a bounded
search, never proof of an absolute first. Historical records establish only an
`assistant` role unless a separate identity mechanism verifies who occupied
that role. M6.1 deliberately does not assemble events across conversations.

M6.2 adds no records table or migration. The adaptive read path can attach an
`echo-pact-event-timeline-v1` response annotation over the evidence selected by
the bounded recall plan. Mention classification is multi-label. Every source
record stays independent: same-day proximity and lexical similarity do not
create event identity, and cross-conversation automatic linking remains off.
Corrections, denials and conflict-group records are retained even if the normal
timeline-node budget is exhausted.

The annotation separates source-message time (`mentioned_at`), an event instant
explicitly bound in source wording (`occurred_at`), and the query-scope earliest
observed mention (`archive_first_mentioned_at`). UTC remains the comparison and
storage form; `Asia/Shanghai` is an additional display conversion. A date-only
source value keeps day precision, and relative dates stay unresolved without a
reliable timezone-aware anchor. The aggregate is explicitly non-exhaustive and
uses `at_least` count semantics. Generator/input hashes make the annotation a
rebuildable derived cache rather than mutable evidence.

When a query explicitly requests original wording, original messages or
verbatim evidence, Echo Pact can make one bounded source-tracing pass after the
normal precision-first lookup. It extracts literal snippets only from the
first-pass records, follows them with escaped parameterized `LIKE`, and keeps
the same visibility predicate inside SQL. The pass neither invents topic
synonyms nor treats the oldest match as adjudicated truth. A successful trace
is labelled `sqlite_original_wording_trace`; if no distinct source is found,
the ordinary recall result and mode are retained.

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

Identity-filtered recall keeps that visible set inside SQLite as a
bounded-parameter subquery. It must not expand one bound variable per visible
record: a principal may own more records than SQLite's per-statement variable
limit, and both recall ranking and coverage must continue to use the same SQL
visibility predicate at that scale.

Direct Claim, provenance, and conflict reads deliberately hide an inaccessible
object as absent (or omit it from lists) so callers cannot probe its existence.
Projection-aware recall uses a `restricted` placeholder only after the caller
has already recalled visible evidence; it signals that a derived interpretation
also depends on evidence the caller cannot read. The asymmetric shape is an
intentional existence-protection boundary, not a difference in authorization.

The local admin registry is append-only: both `agent_id` and `display_name` are
immutable after registration, so a naming mistake requires a new agent. Every
`set-owner` event opens a new authorization epoch, including a repeated event
for the same owner, and therefore invalidates grants from the earlier epoch.
Legacy `/api/recall` clients that used a non-`default` body `agent_id` must move
to a Bearer-derived identity; the request body can no longer select a namespace.

## Knowledge coverage

`verified_knowledge_cutoff_at` is the latest cutoff declared by a verified,
non-`recent_patch` record. `latest_imported_record_at` is the newest record
timestamp, regardless of verification. An unverified `recent_patch` after the
cutoff can be returned but cannot move the verified cutoff.

When `as_of` is later than the verified cutoff, `coverage_status` is
`outside_verified_cutoff` and `coverage_gap` is true. Without `as_of`, temporal
coverage is deliberately reported as `not_assessed_without_as_of`; the system
does not guess a date from free-form query text.
