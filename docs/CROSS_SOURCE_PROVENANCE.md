# Cross-source recall provenance

M12 lets one bounded Echo Pact recall return evidence imported from both the
ChatGPT Room Ferry adapter and the local Codex rollout adapter while keeping
their source identities separate. It is a query-time annotation over immutable
records, not a database merge or a second index.

## Contract

Every adaptive recall response contains `source_provenance` with:

- one source summary per `source_kind + authority + verified` boundary;
- the source channel (`chatgpt`, `codex_local`, or an unclassified `other`);
- record and conversation IDs for audit without repeating message content;
- zero or more high-precision exact cross-source mirror candidates;
- returned records that were not assigned to a mirror candidate;
- explicit policies preventing semantic auto-merge or automatic truth choice.

The annotation covers only the public `memories` returned after the caller's
limit. It does not claim to inventory every matching row in the database.

## Exact mirror candidate

A message can enter `exact_cross_source_mirror_groups` only when all of these
conditions hold:

1. at least one record is from `room-ferry-backup-v1` and one is from
   `codex-app-server-v1`;
2. role is identical;
3. NFKC-normalized content with whitespace collapsed is identical;
4. the normalized text is at least 32 characters and has at least 12 distinct
   alphanumeric characters, excluding generic replies such as `yes` or `嗯`;
5. timezone-aware source timestamps differ by no more than one second.

These rules recognize a high-precision candidate that the same message payload
was preserved by two source archives. They do not prove that two messages
describe the same real-world event, that their content is true, or that an
unverified archive has become verified. Members of one mirror group therefore
count as one support unit rather than independent corroboration.

Semantic similarity, same-day proximity, later retelling, and matching topic
words never create a mirror group. Such records remain separate and appear in
`unresolved_record_ids`; higher-level event logic must continue to describe
them as bounded evidence rather than silently merging them.

## Source separation and shared recall

Room Ferry and Codex adapters continue to emit different `source_kind`,
`source_ref`, `conversation_id`, authority, and verification fields. Importing
both approved packages into one database generation makes them searchable by
the same identity-filtered recall call; it does not erase those boundaries.

The local MCP gateway and cloud snapshot launcher use the same recall code. A
verified snapshot of the same database generation therefore returns the same
`source_provenance` packet on both paths. Snapshot verification proves byte and
schema integrity, not the historical truth of unverified records.

There is no automatic live synchronization and no remote-to-local write path.
A real Codex package still requires a separate, explicit import decision, and a
cloud update still requires creation, verification, transfer, and activation of
a new immutable snapshot generation.

## Privacy and verification

`source_provenance` contains IDs and metadata but no duplicated message text or
content hash. MCP output bounding preserves the annotation unchanged. Automated
tests use synthetic ChatGPT/Codex records and compare local and cloud-launcher
results byte-for-byte at the structured-data level while checking that both
source and snapshot database hashes remain unchanged.
