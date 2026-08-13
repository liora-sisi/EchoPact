# M4.5 / M5 milestone audit and protected-branch release

This milestone closes at migration v6. The evidence table, branch membership,
projection runs, claim/evidence links, and claim version history are protected
by SQLite triggers. A projection claim may only make the one legitimate
`active -> superseded` transition to its immediate next version. Existing
inconsistent projection history makes the v6 migration fail and roll back.

## The three tools

- `scripts/milestone_audit.py` checks the fixed, linear commit ledger; the one
  expected new API route; migrations and required tables/triggers; the full
  test suite; and the ten-step synthetic identity rehearsal.
- `scripts/recovery_bundle.py` creates a new, non-overwriting package with a
  full-history Git bundle, manifest, recovery instructions, SHA-256 list, and
  an independent temporary restore/fsck rehearsal. Its remote snapshot comes
  from `git ls-remote --heads`, never stale local tracking refs.
- `scripts/pre_ff_acceptance.py` checks seven gates and emits evidence. It has
  no push operation. Even a passing result still requires separate authority
  for the exact protected refspec.

All output paths are create-only. Existing evidence or recovery packages are
never overwritten. Reports contain hashes, paths, counts, and test summaries;
they do not contain API keys, credentials, real environment values, or private
record content.

If this Windows host needs its existing local Git proxy for a read-only remote
query, set `ECHO_PACT_GIT_HTTP_PROXY` for that one process to a loopback URL
such as `http://127.0.0.1:7897`. The tool rejects non-loopback proxy values and
never changes permanent Git configuration.

## Normal release order

1. Work on the non-protected `m45-hardening-2026-08-11` branch and make a clean
   checkpoint commit.
2. Run the milestone audit against the fixed base and exact target commit.
3. Create two repository-external packages: the current remote protected head
   (rollback anchor) and the target commit (forward recovery).
4. Run pre-FF acceptance. It re-queries every remote head and refuses drift,
   dirty worktrees, upstream configuration, failed tests/rehearsal, invalid
   recovery packages, non-fast-forward ancestry, or a wrong protected ref.
5. Ask Liora for one exact protected-branch update. If authorised, use only the
   fixed refspec printed by G7, then re-query the remote heads. Never force.

## Failure and rollback

- A v6 migration failure rolls back v6 in the same transaction. The database
  remains at its prior schema version. Do not edit a real database in place;
  back it up, rehearse on the copy, and run the read-only audit first.
- A failed gate performs no protected-branch write. Resolve the named drift or
  failure on the work branch and rerun all gates.
- If a later authorised fast-forward must be reversed, do not force the remote.
  Restore the rollback package into a separate repository, inspect it, and
  obtain explicit approval for the chosen recovery operation.

## Examples

```powershell
python scripts/milestone_audit.py --repo . `
  --expect docs/milestone-m45-m5-expect.json `
  --from-sha 63c3d75192416848382e5697c6366ed48d2df3e1 `
  --to-sha <exact-target> --out <new-audit.json>

python scripts/recovery_bundle.py create --repo . --remote origin `
  --head <exact-head> --snapshot-name <unique-name> --out <new-package-dir>

python scripts/pre_ff_acceptance.py --repo . `
  --expect docs/milestone-m45-m5-expect.json --audit <audit.json> `
  --baseline-package <baseline-dir> --target-package <target-dir> `
  --remote origin --protected-ref refs/heads/v1-core-2026-08-08 `
  --target <exact-target> --out <new-pre-ff.json>
```
