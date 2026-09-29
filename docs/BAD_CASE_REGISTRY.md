# Private bad-case registry and regression ledger

The Telegram `/badcase` packets are immutable evidence.  A repaired case is
not deleted or rewritten.  M16 keeps three private layers outside the Git
repository:

```text
bad_cases/                     # original bad-case-*.json packets (immutable)
bad_case_registry/
  cards/                       # structured diagnosis and lifecycle sidecars
  replays/<case-id>/           # append-only, content-hashed replay summaries
```

The registry refuses to operate when either private root is inside the
repository.  Each card pins the SHA-256 of its original packet; later raw-file
changes fail closed instead of silently changing the regression baseline.

## 1. Register new raw cases

```powershell
python scripts/bad_case_cli.py init `
  --bad-case-dir D:\private\bad_cases `
  --registry-dir D:\private\bad_case_registry
```

Registration assigns `open`, records conservative issue-family suggestions,
and does not copy the private query into the card.  Suggestions are routing
hints, not diagnoses.  A reviewer can replace them explicitly:

```powershell
python scripts/bad_case_cli.py review BAD_CASE_ID `
  --bad-case-dir D:\private\bad_cases `
  --registry-dir D:\private\bad_case_registry `
  --status triaged `
  --family temporal.first_occurrence `
  --layer query_planning --layer ranking `
  --note "manual review confirmed oldest-first intent"
```

## 2. Encode regression expectations

Record IDs are added only after a person has identified the correct evidence.
They can require any one target, every target, and/or forbid known distractors:

```powershell
python scripts/bad_case_cli.py review BAD_CASE_ID `
  --bad-case-dir D:\private\bad_cases `
  --registry-dir D:\private\bad_case_registry `
  --expect-any EXPECTED_RECORD_ID `
  --forbid KNOWN_DISTRACTOR_ID `
  --note "targets verified in /why"
```

## 3. Replay against the current read-only database

```powershell
python scripts/bad_case_cli.py replay all `
  --bad-case-dir D:\private\bad_cases `
  --registry-dir D:\private\bad_case_registry `
  --db D:\private\echo.sqlite3 `
  --agent-id agt-local-reader `
  --limit 10
```

Replay uses the production read-only recall path.  Its stored summary contains
record IDs, source labels, coverage, and SHA-256 fingerprints of previews, but
does not duplicate recalled private text.  Cases without reviewed expectations
remain `unscored`; they are observations, not false passes.

## 4. Keep fixed cases visible

```powershell
python scripts/bad_case_cli.py review BAD_CASE_ID `
  --bad-case-dir D:\private\bad_cases `
  --registry-dir D:\private\bad_case_registry `
  --status fixed `
  --fix-ref commit:abc123 `
  --summary "oldest-first temporal plan" `
  --note "reviewed replay passed"
```

`fixed` requires a fix reference.  The card, its history, original packet, and
all replay summaries remain in reports, so later changes can reveal regression.

```powershell
python scripts/bad_case_cli.py report `
  --bad-case-dir D:\private\bad_cases `
  --registry-dir D:\private\bad_case_registry
```

The default report omits queries.  `--include-query` is an explicit local-only
option and its output must be treated as private.
