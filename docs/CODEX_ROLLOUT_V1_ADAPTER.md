# Codex rollout v1 adapter

## Purpose and current boundary

`codex-app-server-v1` is Echo Pact's source adapter for local Codex task
history. It uses Codex's documented app-server protocol rather than reading or
reverse-engineering the internal SQLite/JSONL stores. The collector first uses
[`thread/read`](https://developers.openai.com/codex/app-server) for the exact
stored thread metadata. Legacy histories are then read with
`includeTurns=true`; paginated histories are hydrated in ascending, bounded
`thread/turns/list` pages with `itemsView=full`. Neither path resumes the
thread. The paginated method currently requires the app-server's explicitly
negotiated `experimentalApi` capability; the collector opts into that
capability but still calls only the reviewed read methods listed below.

The v1 path has two explicit stages:

1. capture one or more reviewed thread ids into a private, checksummed snapshot;
2. dry-run and convert that snapshot to `echo-pact-records-v2` for the existing
   offline importer.

This is a bounded snapshot workflow, not automatic live synchronization and
not an account-wide Codex export feature. It is also separate from any future
agent-authored memory or annotation channel.

## Acquisition boundary

The collector calls only `initialize`, `initialized`, `thread/read`, and when
required `thread/turns/list`, for the exact thread ids supplied on the command
line. It does not call `thread/list`, does not resume a thread, and does not
start a turn. One capture is limited to 50 explicitly reviewed ids, 10,000
turns per thread, and 1,000 turn pages per thread. Repeated cursors, malformed
pages, or a thread changing during pagination fail closed.

The private snapshot contains the complete returned thread objects so source
evidence remains available for local audit. It can therefore contain message
text, task names, local paths, and Git metadata. Keep it outside the repository
and do not upload it as an issue, build artifact, or test fixture.

Capture refuses to overwrite an existing file and writes a new file atomically:

```powershell
python scripts/capture_codex_rollout.py `
  --thread-id EXACT_THREAD_ID `
  --output D:\private\echo-pact\codex-rollout-2026-09-14.json
```

Repeat `--thread-id` to capture more than one reviewed task. The command prints
only the destination, byte/thread counts, and hashes; it does not echo message
text.

## Snapshot envelope

The collector writes one strict UTF-8 JSON object:

```json
{
  "format": "echo-pact-codex-rollout",
  "formatVersion": 1,
  "capturedAt": "2026-09-13T16:00:00.000Z",
  "codexCliVersion": "codex-cli 0.153.4",
  "sourceMethod": "thread/read",
  "acquisitionMethods": ["thread/read", "thread/turns/list"],
  "checksumAlgorithm": "SHA-256",
  "checksum": "sha256-of-canonical-threads-array",
  "threads": []
}
```

The checksum is SHA-256 over the UTF-8 `threads` array serialized with sorted
object keys, no presentation whitespace, and no non-finite numbers. The
adapter verifies it before formal conversion.

## Conversation projection

The adapter converts only visible conversation items:

- `userMessage` becomes role `user`;
- `agentMessage` becomes role `assistant`;
- commentary and final-answer phases are both retained as separate messages;
- text is preserved exactly, including Unicode and line breaks;
- image, audio, skill, and mention inputs become type-only placeholders.

Media URLs, local media paths, skill paths, and mention paths are deliberately
not copied into the record content. Reasoning, plans, commands, command output,
file changes, tool calls, web searches, and other operational items are not
imported or indexed. A future user-input type fails closed until reviewed;
ordinary non-conversation item types are counted and ignored.

## Recall phase preference

Both retained assistant phases remain first-class records.  For ordinary
queries, `final_answer` is a bounded tie-break preference among otherwise
comparable Codex candidates.  Queries that explicitly ask for construction,
debugging, or step-by-step process instead prefer `commentary`.  This does not
filter either phase, override relevance/date/compact-answer intent, or move
non-Codex sources out of their neutral position.  Recall debug output exposes
both the stored phase and the preference selected for the query.

User messages use the turn's original `startedAt`. Assistant messages normally
use the turn's original `completedAt`. One narrow historical exception is
allowed: when an interrupted turn preserved visible assistant commentary but
has no `completedAt`, the adapter uses that same turn's original `startedAt` as
a conservative lower-bound time, emits `assistant-time-fallback`, and marks the
record source reference with `timeBasis=interrupted-turn-start`. It never
substitutes capture time for a missing message time. Other missing times,
in-progress turns, and turns whose items are not fully loaded fail the dry-run.

## Stable identity and forks

Each Codex thread is one Echo Pact conversation and one ordered branch.
Record identity hashes the source thread, turn, and item ids, so retrying the
same snapshot is idempotent and a later snapshot of the same unchanged item
does not duplicate it.

A Codex fork has its own thread id and therefore remains a separate Echo Pact
conversation. V1 does not guess that copied history across two forked threads
is one shared branch. The `source_ref` retains the exact thread/turn/item route
for local source tracing.

If a later full snapshot inserts an older turn and changes already-imported
branch positions, do not force it into the active database. Build a separate
database generation from the newer complete snapshot and retain the older
generation as the rollback point, matching the existing Room Ferry evidence
immutability rule.

Converted rows use:

- `source_kind=codex-app-server-v1`;
- `verified=false`;
- `authority=codex-local-unverified-archive`;
- `source_cutoff_at=capturedAt`.

This preserves source evidence without claiming that historical assistant text
is verified fact or current agent-authored memory.

## Dry-run, conversion, and temporary import

Always dry-run first. The report contains counts, hashes, warning/fatal codes,
and privacy flags only; it never contains message text, cwd, task names, or
local media locations.

```powershell
python scripts/adapt_codex_rollout.py PRIVATE_SNAPSHOT.json --dry-run
python scripts/adapt_codex_rollout.py PRIVATE_SNAPSHOT.json --output RECORDS_V2.json
python scripts/import_history.py RECORDS_V2.json `
  --db TEMPORARY_COPY.sqlite3 `
  --owner-agent-id REVIEWED_AGENT `
  --actor codex-rollout-v1
```

Formal conversion refuses an existing output path and atomically creates the
new records package only after a clean dry-run. The current milestone must be
rehearsed against a temporary database; it does not authorize writing a real
Echo Pact database.

## Safe refusal conditions

Conversion is refused for malformed/non-UTF-8 JSON, files over 500 MB, wrong
format/version/source method, checksum mismatch, missing or duplicate source
ids, partial item views, in-progress turns, missing original turn times,
unknown assistant phases, unreviewed user-input types, or malformed visible
message content.

The committed fixture `tests/fixtures/codex_rollout_v1.json` is entirely
synthetic. Tests cover redacted dry-run, bounded exact-id capture, checksum
failure, schema drift refusal, media-path stripping, deterministic conversion,
idempotent import, offline recall, and index consistency without opening any
real Codex task or private Echo Pact database.

## Private real-thread acceptance (2026-09-14)

After the synthetic suite passed, one explicitly reviewed local Codex thread
was captured through the bounded app-server path and exercised entirely inside
an isolated private acceptance directory. No account-wide enumeration was
performed, no thread was resumed, and the active Echo Pact database was not a
write target.

The redacted acceptance evidence was:

- targeted test suite: 23 passed;
- capture: 1 thread, 350 turns, 4,730 items, 59,362,690 bytes;
- projection: 365 user messages and 1,590 assistant messages became 1,955
  records; 2,775 operational items were excluded;
- safety report: 0 fatal issues, 6 `assistant-time-fallback` warnings, valid
  snapshot checksum, and no message text or media locations reported;
- converted package: 1,955 records, 2,908,993 bytes;
- isolated import: 1,955 added and 0 failed;
- identical replay: 0 added and 1,955 skipped as an idempotent replay;
- index audit: 1,955 records, state rows, FTS documents, and branch membership
  records with no missing, orphaned, or stale entries;
- read-only recall: a reviewed phrase matched records from
  `codex-app-server-v1` with authority
  `codex-local-unverified-archive`; recalled message text was not printed.
- full content audit: all 1,955 projected records matched their source message
  text, role, and assistant phase exactly, with no missing or unconverted
  visible messages; this included 1,242 commentary messages and 348 final
  answers, and the audit reported only counts plus an aggregate digest.

The private snapshot, converted package, and sandbox database remain outside
the repository. This acceptance proves the bounded path can carry one selected
real thread into an isolated Echo Pact generation; it does not authorize
automatic synchronization or import into the active private database.
