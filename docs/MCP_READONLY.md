# Echo Pact local read-only MCP gateway

M6-01 exposes the existing identity-filtered SQLite recall as a local MCP
server.  It is an adapter, not a second memory database.

## Safety contract

- STDIO only: the process does not listen on a network port.
- Tools are limited to `recall_context` and `memory_coverage`.
- Both tools advertise MCP read-only, non-destructive and closed-world hints.
- The agent identity is fixed at process startup.  Tool callers cannot provide
  or switch `agent_id`.
- SQLite is opened with `mode=ro` and `PRAGMA query_only`.
- The gateway never runs migrations.  A database not migrated to the exact
  supported schema version is rejected without alteration.
- Results retain source, branch, verification, authority, conflict and cutoff
  metadata.  Long text is bounded per result and explicitly marked truncated.
- No embedding API or other network request is used.

## Required local configuration

Two environment variables are mandatory:

```text
ECHO_PACT_MCP_DB_PATH=<absolute path to an existing records_v1 SQLite database>
ECHO_PACT_MCP_AGENT_ID=<an active agent registered in that database>
```

Do not put credentials, API keys or Bearer secrets in either value.  An agent
ID is an authorization principal, not a secret; database visibility rules still
decide which evidence it can read.

Start the server from the repository root with the project's existing Python:

```powershell
.\.venv\Scripts\python.exe -m backend.mcp.readonly_server
```

For Codex configuration, prefer the location-independent launcher
`scripts/echo_pact_mcp.py`; it works even when the client starts it from a
different working directory.

The process waits for MCP JSON-RPC on stdin.  It must not print ordinary logs to
stdout because stdout is reserved for the protocol.

## Codex configuration

Codex can start this as a local STDIO server.  Use absolute paths in the actual
user configuration; keep private database locations out of committed files.

```toml
[mcp_servers.echo_pact]
command = "D:\\path\\to\\EchoPact\\.venv\\Scripts\\python.exe"
args = ["D:\\path\\to\\EchoPact\\scripts\\echo_pact_mcp.py"]
startup_timeout_sec = 20
tool_timeout_sec = 60
required = false
enabled_tools = ["recall_context", "memory_coverage"]

[mcp_servers.echo_pact.env]
ECHO_PACT_MCP_DB_PATH = "D:\\private\\path\\records.sqlite3"
ECHO_PACT_MCP_AGENT_ID = "agt-local-reader"
PYTHONDONTWRITEBYTECODE = "1"
```

After adding or changing the configuration, restart the local Codex client (or
start a new session) and inspect `/mcp`.  ChatGPT web uses a separately deployed
remote MCP/plugin and does not read this local Codex configuration.

## Tool semantics

The host should treat Echo Pact as a fallback for missing context, not as a
mandatory preamble to every conversation about the past. It should prefer the
current conversation when that context is already sufficient. It should call
`recall_context` when user-specific history, an exact quotation/date, or the
origin of a past artifact matters and current context or reliable memory is
insufficient. Before answering from a vague impression, saying it does not
remember, or asking the user for a hint, the host should try one bounded recall
when the archive could help. Words such as image, drawing, photo, song, or gift
do not turn a past-memory question into a creation request unless the user
explicitly asks to create or edit content.

### `recall_context`

Inputs: `query`, optional `limit` (1-10), optional `as_of`, and optional
`include_projection`.  The response preserves Echo Pact's evidence and coverage
contract.  `confidence` remains a deterministic evidence score, not a
probability.  Unverified archive records do not become confirmed facts merely
because they were recalled. The underlying SQLite recall is precision-first:
exact phrases precede deterministic natural-language relaxation, and the
returned `recall_mode` identifies the tier that actually supplied the result.
Where ordered branch positions exist, each match may include a bounded
`conversation_context` window with full provenance. MCP truncation limits are
applied to the match, its context records and any `event_evidence` records.

One public call may contain a bounded adaptive plan. Exact anchors keep the
single-pass fast path. Meaning questions require an entity and explanatory
language in the same record. Source-sensitive questions can add deterministic
original-source tracing and a small source-neutral vocabulary expansion, up to
`MAX_ADAPTIVE_QUERY_PASSES`. The response reports this under
`adaptive_recall`; no private-fact dictionary, network model, database write,
or unbounded retry loop is involved. Shared-event scans are not expanded a
second time, so an unsupported negative control remains empty instead of
drifting into unrelated shared history.

Quoted wording, artifact titles, explicit ASCII names/codes and numeric room
labels are treated as caller-supplied evidence anchors. For an explicit
enumeration, the strongest quoted/title handle is required while the remaining
literal items are bounded ranking bonuses; this lets a record matching the
song, place and objects outrank title-only noise without requiring every detail
to occur in one message. Explicit wording also receives a slightly wider but
still bounded same-branch context window so the prompt, answer, correction and
later reaction can travel together.

When the query itself contains a valid calendar date that is strictly later
than `latest_imported_record_at`, the response adds `temporal_coverage`, clears
older lexical matches and reports `sqlite_temporal_coverage_guard`. This is a
fail-closed archive boundary, not an inference about what happened later.

An explicit preference subject is kept together with generic
preference/choice/answer language. A query marked as "last" or "most recent"
with multiple literal subjects requires all those subjects in the evidence and
orders qualifying rows newest first. Both remain a single read-only SQLite pass
when they succeed; neither uses a private-fact dictionary or supplies an
expected answer.
For this latest composite-event tier, a conversational "what did you say then"
suffix does not override the event constraints with a global wording trace;
the bounded same-branch context supplies the historical reply instead.

An earliest question that explicitly asks about a shared two-person event may
use `recall_mode=sqlite_shared_event_window`. This mode runs one bounded,
identity-filtered, same-conversation and same-branch rescue pass. It requires
literal topic and dyadic-participation evidence, rejects non-events and
incompatible event objects, and returns the candidate boundary in
`event_recall`. Its `partial_support` and
`historical_assistant_role_only` statuses are intentional: the result is not
proof of an absolute first or of the historical assistant's present identity.
The gateway does not assemble event evidence across conversations.

Queries that explicitly ask for original wording, original messages or
verbatim evidence may perform one additional offline source-tracing pass. The
pass extracts a bounded set of literal snippets from the first-pass evidence
and searches those snippets with parameterized SQLite `LIKE`, under the same
agent-visibility predicate and result limit. It does not generate synonyms,
call a model, use the network or loop indefinitely. A successful pass reports
`recall_mode=sqlite_original_wording_trace`; the returned records remain
evidence candidates rather than an automatic historical adjudication.

### `memory_coverage`

Returns the visible record count, verified knowledge cutoff, latest imported
record time and recent-patch boundary for the fixed agent, without returning
conversation text.

## Verification

All automated tests use temporary databases and synthetic records.  A private
database smoke test must compare its SHA-256 before and after the MCP calls and
report only counts/metadata, never the recalled conversation body.
