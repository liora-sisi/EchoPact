# Echo Pact always-on read-only deployment

## 1. What this adds

This is a deployment boundary around the existing read-only MCP gateway, not a
second writable memory system.

```text
local writable authority
  -> SQLite online backup
  -> immutable versioned snapshot + manifest
  -> encrypted one-way transfer
  -> remote verification
  -> atomic active pointer
  -> existing read-only MCP
  -> OpenAI private tunnel
  -> ChatGPT / Work
```

The local Echo Pact database remains authoritative. The remote copy is a
replaceable read-only serving generation. There is no remote-to-local merge and
no two-way database synchronization.

## 2. Safety contract

- Snapshot creation opens the source with SQLite `mode=ro` and copies it with
  SQLite's online backup API, so committed WAL content is included consistently.
- Every release has a canonical `manifest.json` containing only schema, size,
  SHA-256, record counts, coverage boundaries, fixed agent identity and
  creation time. Conversation text is never written to the manifest or CLI
  report.
- Verification requires the exact supported migration chain, `quick_check=ok`,
  byte size/hash equality and the expected active agent.
- Release directories are never overwritten. A failed build removes only its
  own `.building-*` directory.
- Activation verifies the candidate first, atomically replaces a small pointer
  file, and preserves one verified previous pointer. Rollback swaps those two
  pointers after verifying both generations.
- `scripts/echo_pact_cloud_mcp.py` refuses to start if the pointer, release,
  hash, schema or agent is wrong. It then delegates to the existing MCP server,
  which still opens SQLite read-only with `PRAGMA query_only`.
- The OpenAI tunnel makes an outbound encrypted connection. The deployment does
  not need a public MCP listening port. Tunnel credentials belong in a
  root-readable environment file or secret store, never in Git or a profile.
- A snapshot still contains private conversation text. It must remain on
  Liora-controlled storage, use restrictive filesystem permissions and never be
  attached to tickets, logs, Git commits or public object storage.

## 3. Create and verify a release

Run from a trusted Echo Pact checkout with the matching code/schema version:

```bash
python scripts/echo_pact_cloud_snapshot.py create \
  --source-db /private/echo-pact/current.sqlite3 \
  --release-root /private/echo-pact/releases \
  --agent-id agt-example-reader

python scripts/echo_pact_cloud_snapshot.py verify \
  --release-dir /private/echo-pact/releases/snapshot-... \
  --agent-id agt-example-reader
```

Before transfer, record the snapshot ID, byte size and SHA-256 from the
metadata-only output. Transfer exactly the new release directory over an
encrypted authenticated channel. Do not expose an HTTP download URL. On the
remote host, run `verify` again and compare all three values before activation.

Creating or transferring a snapshot of the real private database is an explicit
privacy boundary: confirm the exact source, destination host, remote path,
permissions and rollback generation before doing it.

## 4. Activate, start and roll back

```bash
python scripts/echo_pact_cloud_snapshot.py activate \
  --release-dir /var/lib/echo-pact/releases/snapshot-... \
  --pointer /var/lib/echo-pact/active.json \
  --agent-id agt-example-reader

ECHO_PACT_MCP_SNAPSHOT_POINTER=/var/lib/echo-pact/active.json \
ECHO_PACT_MCP_AGENT_ID=agt-example-reader \
PYTHONDONTWRITEBYTECODE=1 \
python scripts/echo_pact_cloud_mcp.py
```

The MCP launcher validates the pointer at each process start. After activation,
restart only the tunnel/MCP service and perform `memory_coverage` plus a bounded
synthetic or already-approved recall smoke test. If the new generation fails:

```bash
python scripts/echo_pact_cloud_snapshot.py rollback \
  --pointer /var/lib/echo-pact/active.json \
  --agent-id agt-example-reader
```

Restart the service and verify the reported active snapshot ID. Do not delete
the failed or previous release during incident handling.

## 5. OpenAI private tunnel

`deploy/cloud/tunnel-profile.yaml.example` and
`deploy/cloud/echo-pact-mcp.service.example` are secret-free templates. Supply
the existing OpenAI tunnel ID and control-plane key only on the deployment host.
The profile launches `scripts/echo_pact_cloud_mcp.py`; it does not point directly
at a writable database.

The tunnel client binary and its exact service path are host-specific and are
not vendored here. Run its `doctor` command before starting the service. A
deployment is accepted only when:

1. the active release verifies locally on the host;
2. the private tunnel reports ready;
3. `memory_coverage` matches the snapshot manifest;
4. the same fixed synthetic query through the local gateway and cloud launcher
   returns identical records, provenance, adaptive-recall metadata, event
   timeline and coverage boundaries;
5. an explicit query beyond imported coverage returns no older lexical match,
   reports `coverage_gap`, and suppresses the event timeline on both paths;
6. a bounded read returns no write capability and the database hash is unchanged;
7. the prior pointer can be restored and verified.

## 6. Updating cloud data

Updates are generation-based rather than live two-way sync:

1. finish/import data into the local authoritative database;
2. create a new snapshot while the app may remain online;
3. verify locally;
4. transfer the new, uniquely named release;
5. verify remotely;
6. activate the pointer and restart the service;
7. compare coverage and retain the previous generation.

This keeps the phone-visible archive fresh without letting a remote process edit
the local authority. Scheduling and retention can be added after one manual real
snapshot has completed an approved restore/rollback rehearsal.
