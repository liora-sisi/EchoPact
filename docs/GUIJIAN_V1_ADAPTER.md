# GuiJian browser-capture export v1 adapter

`guijian-browser-capture-v1` converts one offline GuiJian ChatGPT-room export
into Echo Pact's compact `echo-pact-records-v2` format. It is a temporary
bridge while an official account export is unavailable. It never treats a
browser observation as an official ChatGPT record.

The adapter does not open an Echo database, call a model, use the network, or
copy the source file. A dry-run reports only hashes, counts, time-evidence
classes, and issue codes; it does not print the room title, room key, or
message text.

## Accepted source contract

The input must be one strict UTF-8 JSON document with
`exportSchemaVersion = web-memory-ferry/export-v1`, platform `chatgpt`, and
authority status `provisional-pending-official`. Formal conversion requires:

- both the raw observation layer and normalized message layer;
- a lowercase SHA-256 fingerprint for each raw observation;
- raw fingerprints identical to normalized `contentHash` values in order;
- a valid manifest `sequenceHash` over the normalized hashes;
- zero-based, consecutive normalized indexes and valid raw segment indexes;
- visible-top and quiet-DOM confirmation, no declared gaps, and no unresolved
  role warnings;
- timezone-aware capture and observation timestamps; and
- no backward movement among direct message-time anchors.

Equal message bodies are preserved as separate ordered observations. Content
equality is not message identity, so the adapter never performs global content
deduplication.

If a new otherwise-continuous capture does not set `topConfirmed`, the caller
may explicitly provide one older same-room export as `--top-witness`. The
older export must itself have confirmed the visible top, must pass all
integrity gates other than gaps or direct-time monotonicity elsewhere in that
old capture, must predate the new export, and must share at least six leading
message fingerprints with the new capture. This historical witness can satisfy
only the top boundary; none of its messages, gaps, or timestamps are copied
into the new package.

When the capture operator directly observed the visible top and recognized the
first message, `--attest-visible-top` records that evidence instead of
pretending GuiJian set `topConfirmed`. The attestation is bound to the exact
input SHA-256 and first selected message fingerprint, contains no message text,
and remains visibly distinct from automatic or historical-witness evidence.

Some virtualized pages can produce multiple capture segments even when one
later segment is a complete ordered superset of every earlier segment. The
explicit `--repair-redundant-segments` option accepts this case only when one
raw segment provably contains all other fingerprint sequences in order and its
normalized messages match exactly. The package records the selected segment
and discarded observation counts. This is segment-level evidence repair, not
global content deduplication; ambiguous or complementary segments still fail.

Long virtualized rooms can also repeat the same ChatGPT message across many
capture windows. The explicit `--repair-source-message-ids` option is a
separate, stricter repair path. It requires a canonical ChatGPT
`data-message-id` on every raw observation, identical role and fingerprint for
every occurrence of one ID, a clear 3:2 majority for any reciprocal adjacency
noise, and exactly one topological ordering after that noise is removed. It
collapses observations by source identity, never by equal message text. A
missing ID, changed body, tied or weak edge, cycle, or more than one possible
order fails closed. The report retains only counts and repair evidence; raw
message IDs are not printed.

## Time evidence

The adapter uses this precedence for `created_at`:

1. a timezone-aware platform-time candidate included by GuiJian;
2. an assistant message whose first line strictly matches
   `YYYY-MM-DD HH:mm（成都）`;
3. the next direct self-stamp as a conservative upper bound for an earlier
   unstamped message; or
4. the message's capture observation time as a trailing upper bound.

The exact basis and precision remain in `source_ref` as `timeBasis` and
`timePrecision`. Upper bounds are labelled `upper-bound-only`; they are not
presented as original platform event times. All converted records remain
`verified = false`, and their authority names retain the browser-observation
boundary.

## Source separation and future reconciliation

GuiJian records use their own `source_kind` and deterministic conversation,
message, and record identifiers. Recall classifies them under the `chatgpt`
source channel, while the debug panel displays
`GPT库（浏览器抓取）` rather than `GPT库（官方数据包）`.

GuiJian is deliberately excluded from M12 automatic exact-mirror compaction.
An official package may later overlap these observations, but replacing,
deleting, or merging either source requires a separate reconciliation review.
Official authority does not silently rewrite the provisional archive.

## Commands

Validate without creating a formal package:

```powershell
python scripts/adapt_guijian.py GUIJIAN_ROOM.json --dry-run
```

With an explicitly selected historical top witness:

```powershell
python scripts/adapt_guijian.py GUIJIAN_ROOM.json `
  --top-witness OLDER_SAME_ROOM.json `
  --dry-run
```

With direct visible-top review and a provable redundant-segment repair:

```powershell
python scripts/adapt_guijian.py GUIJIAN_ROOM.json `
  --attest-visible-top `
  --repair-redundant-segments `
  --dry-run
```

For a long room whose canonical source IDs prove one unique order:

```powershell
python scripts/adapt_guijian.py GUIJIAN_ROOM.json `
  --repair-source-message-ids `
  --dry-run
```

After reviewing a successful dry-run, create a new package atomically:

```powershell
python scripts/adapt_guijian.py GUIJIAN_ROOM.json --output RECORDS_V2.json
```

The output target must not already exist. Conversion alone does not authorize
import into a real Echo database or deployment to a remote snapshot.
