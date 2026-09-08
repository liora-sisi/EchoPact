# Speech-to-text query rescue

Echo Pact may run a small deterministic retry when speech-to-text noise hides
an otherwise retrievable term. This is a retrieval-only operation: the
original query, stored records, projections and claims are never rewritten.

## First-cut contract

- The public repository contains mechanism and synthetic examples only. It
  contains no private names, nicknames, household vocabulary or answer map.
- A trusted local caller may inject a rebuildable `SpeechQueryRescuePolicy`.
  The policy holds at most 64 observed-to-intended mappings, an optional set of
  protected terms, and permits at most two candidate queries per call.
- The gateway does not load a private overlay automatically in this cut. The
  policy is an explicit in-process dependency, so a missing policy preserves
  the existing recall behaviour and public MCP tool schema.
- A mapping is considered only when its observed form occurs literally in the
  query. A protected span blocks the replacement.
- Rescue is a fallback. It runs when the first pass has no result, or when the
  returned evidence does not contain the observed form. A non-empty but
  unanchored result therefore cannot silently suppress rescue. Evidence that
  literally supports either the observed or intended form prevents a needless
  correction pass.
- Candidate passes share the existing identity filter, time scope, read-only
  SQLite connection and four-pass adaptive budget. They do not call a model or
  network service and do not mutate a database.

## Audit trace

When a policy is present, `recall_context` adds `speech_query_rescue`. The
trace keeps the original query, trigger decision and reason, protected-term
matches, candidate query and mapping, executed pass result counts and record
IDs, plus the candidate passes attached to final selected hits. This makes the
retry inspectable rather than a hidden rewrite.

The trace can contain caller-supplied private vocabulary. Treat it like the
query itself: return it only to the authorized local caller and do not copy it
into public logs, reports or fixtures.

## Synthetic acceptance gate

`tests/test_speech_query_rescue.py` contains a 20-case synthetic matrix:

- 8 transcription errors must produce a rescue candidate;
- 5 protected terms must remain untouched;
- 4 correct colloquial or sarcastic utterances must not trigger;
- 3 image placeholders must not trigger or gain invented visual evidence.

The suite reports these boundaries separately through assertions: rescue is
8/8, false corrections are 0/12, and false triggers are 0/12. These figures
describe only the committed synthetic suite; they are not a global accuracy
claim.

## Later local integration

A future private adapter may derive the overlay from local speech-to-text
correction logs. That artifact must remain outside the repository and core
records database, be rebuildable and auditable, and preserve the same bounded
injection contract. Enabling or refreshing such an adapter is a separate
operational change from this public mechanism.
