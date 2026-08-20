# Real-data acceptance runway

Echo Pact's real-data acceptance is deliberately split into small gates.  The
first gate (A1) validates one Room Ferry full-backup v1 JSON without converting
it, importing it, opening a real Echo Pact database, or using the network.

## A1: redacted read-only preflight

```bash
python scripts/preflight_room_ferry.py ROOM_FERRY_BACKUP.json \
  --report NEW_REDACTED_REPORT.json
```

The command:

- fingerprints the input before and after the existing Room Ferry dry-run;
- validates format, versions, checksum, message fields, time semantics and
  branch recoverability through the existing source adapter;
- creates one new report with aggregate counts and stable issue codes only;
- never overwrites an existing report;
- does not create a records package, write a database, or make a network call;
- does not include the input path, file name, conversation/message IDs, titles,
  message text, source IDs, or free-form adapter error messages.

Exit code `0` means the input is unchanged and eligible for the later
conversion gate.  Exit code `2` means validation failed or the requested report
path is unsafe.  Exit code `1` is reserved for other file-system failures.

The JSON report is evidence, not authorization to continue.  A report with
`decision.can_proceed_to_conversion=true` only says that the archive passed A1.

## What A1 intentionally does not do

A1 does not implement the later A2 acceptance flow.  It does not:

- convert a private archive to `echo-pact-records-v2`;
- import records into a new private v6 database;
- create or test real owner/peer/stranger identities;
- run recall or audit against real records;
- migrate an existing real Echo Pact database.

Those steps must use new outputs outside the repository, preserve the original
archive, and produce only redacted evidence.  They remain a separate milestone
because they require explicit authorization to read the real private archive.

## Current resource boundary

The preflight fingerprints files in 1 MB chunks, but the underlying Ferry v1
adapter currently buffers the complete JSON document while validating and
parsing it.  The 500 MB input ceiling is a protocol safety limit, not a promise
of constant or 500 MB memory use.  Before a large private archive is inspected,
verify that the machine has comfortable free memory and stop other memory-heavy
work.  Streaming JSON validation is a separate future improvement and is not
silently claimed by A1.
