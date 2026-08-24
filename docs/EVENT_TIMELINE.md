# Query-time event and retelling timeline

M6.2 adds a rebuildable `event_timeline` annotation to the existing read-only
adaptive recall response. It does not add a database table, change
`echo-pact-records-v1`, or write to the evidence database.

## Purpose and claim boundary

The timeline lets a caller see the chronology of evidence selected by one
bounded recall plan: an earlier archive mention, later recollections, quoted
wording, explicit detail additions, future plans, and corrections or denials.
`mention_types` is multi-label because one message may be both a retelling and
a quotation, or both a correction and a historical reference.

The timeline deliberately does **not** assert that two similar messages describe
the same real-world event. Each message remains an independent node with
`same_event_status=not_automatically_asserted`. Same-day timestamps, similar
wording, or a shared title are not identity. Cross-conversation event linking
requires a later explicit projection/candidate-link layer and is not performed
here.

`archive_first_mentioned_at` means the earliest source-message timestamp among
the evidence gathered for this bounded response. Its scope is
`bounded_recalled_evidence`, `exhaustive=false`, and its count is reported as
`at_least`. It is not a claim about the absolute first mention in the complete
archive.

## The three clocks

The response keeps three different ideas separate:

1. `mentioned_at` is the source message time. Records-v1 requires a
   timezone-aware ISO-8601 value and normalizes it to UTC (`Z`).
2. `occurred_at` is the event instant. It is populated only when the source
   explicitly binds an event to a timezone-aware timestamp. It is otherwise
   null.
3. `archive_first_mentioned_at` is a query-scope aggregate over the recalled
   evidence, never an inherent property copied onto every message.

For Liora's normal display, `mentioned_at_local` and `occurred_at_local` convert
known instants to `Asia/Shanghai` (`UTC+08:00`). Storage and deterministic
comparison remain UTC. A source calendar date such as `2026年6月1日` can populate
`occurred_on` only when wording explicitly binds it to the event; it does not
become a midnight instant. Relative phrases such as “昨晚” remain unresolved
without a reliable timezone-aware anchor. Import time, Room Ferry export time,
`firstImportedAt`, and `lastImportedAt` are never substituted for event time.

M6.2.1 adds a separate `query_clock` for supported caller-relative phrases.
With a reliable reference instant, `今天`、`昨天`、`前天`、`昨晚`、`上周X`、
`上周` / `上星期` and `上个月` resolve against `Asia/Shanghai`. A missing
reference remains `unresolved_missing_timezone_aware_reference`. The local MCP
gateway supplies its own Chengdu server clock only when a relative phrase is
present and the caller omitted `as_of`. The resolved day/week/month is a query
interpretation aid (`used_for_record_filtering=false`), not evidence that any
returned record describes that event.

ChatGPT export timestamps travel through Room Ferry as epoch milliseconds and
are normalized by Echo Pact to UTC instants. They are not stored as Beijing
wall-clock strings. Local conversion happens only in the response annotation.

## Boundedness and safety

- The normal chronology keeps at most 12 ordinary nodes, preserving both ends
  when truncation is necessary.
- Any gathered node carrying a conflict group or explicit correction/denial is
  retained even when the ordinary-node budget is full.
- Lexical novelty alone is not labelled as a detail addition; the source must
  explicitly say it is adding a detail.
- A qualifying shared-event window may trigger one additional deterministic
  retelling trace. An unsupported event does not receive that expansion.
- A generic "first time we ate together" question may also receive one bounded,
  source-neutral meal-category rescue inside the same external tool call. The
  rescue never contains a private dish, date, restaurant, or expected answer.
- A query outside the imported coverage boundary suppresses the timeline rather
  than exposing older lexical lookalikes.
- The response carries a generator version, input SHA-256 and deterministic
  semantic payload SHA-256. It is a derived cache candidate that can always be
  rebuilt from immutable evidence.

The annotation contains provenance and short evidence excerpts. It performs no
network calls, model inference, private-answer lookup, migration, or database
write.
