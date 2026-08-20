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

The process waits for MCP JSON-RPC on stdin.  It must not print ordinary logs to
stdout because stdout is reserved for the protocol.

## Codex configuration

Codex can start this as a local STDIO server.  Use absolute paths in the actual
user configuration; keep private database locations out of committed files.

```toml
[mcp_servers.echo_pact]
command = "D:\\path\\to\\EchoPact\\.venv\\Scripts\\python.exe"
args = ["-m", "backend.mcp.readonly_server"]
cwd = "D:\\path\\to\\EchoPact"
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

### `recall_context`

Inputs: `query`, optional `limit` (1-10), optional `as_of`, and optional
`include_projection`.  The response preserves Echo Pact's evidence and coverage
contract.  `confidence` remains a deterministic evidence score, not a
probability.  Unverified archive records do not become confirmed facts merely
because they were recalled.

### `memory_coverage`

Returns the visible record count, verified knowledge cutoff, latest imported
record time and recent-patch boundary for the fixed agent, without returning
conversation text.

## Verification

All automated tests use temporary databases and synthetic records.  A private
database smoke test must compare its SHA-256 before and after the MCP calls and
report only counts/metadata, never the recalled conversation body.
