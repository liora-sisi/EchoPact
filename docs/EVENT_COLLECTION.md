# Bounded multi-occurrence event collection

`echo-pact-event-collection-v1` is a read-only response annotation for an
explicitly plural memory question, for example: "how many times did we do X,
when, and who chose the details?"  It does not change `records_v1`, create a
projection, or claim that lexical similarity proves real-world event identity.

## Query plan

The ordinary query remains the first pass.  When the caller explicitly asks
for several occurrences (`几次`, `多少次`, `分别`, `每次`, and bounded related
forms), Echo Pact may use the remaining three adaptive-pass slots for:

1. the literal activity subject;
2. one public-language surface form when the source archive commonly uses a
   different wording;
3. choice, design, advice, decision, and later-retelling language bound to the
   literal subject.

The complete plan remains within `MAX_ADAPTIVE_QUERY_PASSES`.  Each collection
pass may inspect up to 24 local candidates so a large archive is not reduced
to one noisy top-ten page; the public `memories` limit remains unchanged and
the deduplicated evidence packet is still capped at 24 records. It is
deterministic, identity-filtered and offline.  It does not use a model, a
private expected answer, the network, or unbounded retries.

## Response boundary

The optional `event_collection` object stores each bounded excerpt once in
`evidence`; category fields contain record IDs instead of duplicating text. It
separates:

- `occurrence_evidence` with explicit occurrence or completion wording;
- `plan_or_candidate_evidence`;
- `choice_or_advice_evidence`;
- `retelling_or_recollection_evidence`;
- conservative `candidate_occurrences`.

`event_count_lower_bound` is **not an exact total**.  It counts only recalled
records that explicitly describe occurrence/completion and are not questions
or later retellings.  Multiple qualifying messages on the same Chengdu source
message day share one conservative lower-bound bucket, which may under-count
two real events on one day but avoids counting every turn as a new event.
`created_at` remains the source-message time; its Chengdu calendar day is a
sorting/grouping proxy, not automatically the event occurrence date.

Retellings are preserved but never counted as new occurrences.  Record IDs are
deduplicated.  Similar text, same-day mentions, or cross-conversation records
are not automatically declared to be the same event.  A host should therefore
say "at least N supported occurrences were found" and explain unresolved
evidence instead of presenting the bounded result as an exhaustive biography.

## Safety

- read-only SQLite retrieval and response-time annotation only;
- all ordinary agent visibility and archive coverage checks still apply;
- no database migration or persistent index;
- no real data in automated tests;
- no silent merge, overwrite, conflict resolution, or verified-cutoff change.
