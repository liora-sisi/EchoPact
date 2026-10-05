# ChatGPT official account-export v1 adapter

`chatgpt-official-export-v1` converts one official ChatGPT account-export ZIP
into Echo Pact's compact `echo-pact-records-v2` format. The ZIP remains
read-only and is never extracted. The adapter does not call a model or use the
network.

## Integrity and source boundary

The dry-run hashes the complete ZIP, validates every member CRC, rejects unsafe
or duplicate member names, and verifies that `export_manifest.json` names the
same files and uncompressed sizes as the ZIP directory. It accepts manifest
version 1 and the logical `conversations.json` file in either reviewed single
file form or consecutive `conversations-NNN.json` shards.

The manifest currently carries sizes rather than cryptographic hashes for each
member. The adapter therefore records the SHA-256 of the complete ZIP in the
package and every record source reference. `source_cutoff_at` is the latest
conversation update or message time actually present in the archive; it does
not claim knowledge after that instant.

## Conversation projection

The adapter preserves all reviewed human-facing conversation layers:

- user `text` and `multimodal_text` become ordinary user records;
- assistant `text` and `multimodal_text` are labelled `phase=final_answer`;
- assistant `thoughts` are retained and labelled `phase=thoughts`;
- assistant `reasoning_recap` is retained and labelled
  `phase=reasoning_recap`;
- audio transcriptions remain text, while image/audio/video assets become
  type-only placeholders without copying asset URLs.

Empty folded-reasoning nodes are reported and skipped. Any new role, content
type, structured media type, malformed timestamp, or broken mapping graph fails
closed until its semantics are reviewed.

All retained layers remain searchable. Ordinary recall uses final answers as a
bounded tie-break preference; explicit process questions can prefer commentary,
thoughts, and reasoning recaps. No phase is filtered, rewritten, or treated as
independent proof merely because it appears in the archive. The debug panel
shows the stored phase for each returned record.

## Stable identity and branches

Official conversation and message IDs define deterministic record identity.
The mapping parent graph is validated as one connected acyclic tree. Every
root-to-leaf path becomes a stable branch membership, with the exported current
leaf distinguished from alternate leaves. Shared ancestors store their content
once and retain every applicable branch membership.

## Commands

Validate without writing a record package:

```powershell
python scripts/adapt_chatgpt_official.py CHATGPT_EXPORT.zip --dry-run
```

After reviewing the redacted report, atomically create a new package:

```powershell
python scripts/adapt_chatgpt_official.py CHATGPT_EXPORT.zip `
  --output CHATGPT_OFFICIAL.records-v2.json
```

The output target must not exist. Conversion does not modify an active Echo
database. A newer complete official export should build a new database
generation beside the previous generation so rollback remains a file switch,
not deletion or in-place evidence rewriting.
