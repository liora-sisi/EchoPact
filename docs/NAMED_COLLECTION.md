# Named collection recall

`named_collection` is a rebuildable, read-only response annotation for plural
inventory questions such as:

> How many bracelets did we choose together, and what are their names?

It is intentionally separate from `event_collection`. A manicure performed
three times is a collection of occurrences; three named books or bracelets are
a collection of items.

## Recognition

The detector requires explicit plural inventory language: a supported count
unit (`个`, `件`, `串`, `本`, `瓶`, and similar), or a names question with a
plural reference such as `它们` or `分别`. Units for occurrences (`次`, `回`)
remain owned by `event_collection`. A singular preference question does not
activate named collection recall.

The intent records:

- the literal public-language subject and optional count unit;
- whether count and names were requested;
- whether the caller explicitly limited the set to jointly selected items.

No private name, date, answer, platform, model, or source adapter is part of
the detector.

## Evidence classes

Within the existing four-pass adaptive budget, Echo Pact gathers bounded
literal subject, public alias, naming, and relationship evidence. It returns:

- `confirmed_items`: explicitly named items whose evidence satisfies the
  requested relationship scope;
- `candidate_items`: candidate, tentative, not-ordered, or not-final names;
- `unresolved_items`: explicit item names whose final-name or requested
  relationship evidence did not close within this bounded packet;
- `evidence`: bounded source rows with provenance and short excerpts;
- `excluded_relation_evidence_record_ids`: matching item evidence explicitly
  outside the requested relationship.

An item name is extracted only from an explicit quoted/bracketed name or direct
naming construction. Similar text is not enough. Explicit source-language
exclusions such as “this necklace must not count as a bracelet” take priority
over a stray lexical match.

## Count and identity boundaries

`named_item_count_lower_bound` counts distinct normalized names in
`confirmed_items`. A later retelling of the same name does not create a new
item, and a candidate does not become confirmed merely because it was recalled.
An unresolved item may be useful to the calling model as a lead, but must not be
silently counted as confirmed.
The response always reports:

```text
exact_total_status = not_proven_by_bounded_recall
```

Therefore callers must say “at least N confirmed named items were found,” not
“there were exactly N.” Unnamed items, missing archive portions, and evidence
outside the bounded result may exist. Cross-conversation unnamed similarity is
never used to merge or invent item identity.

## Safety and portability

The feature operates over the source-neutral Echo Pact record shape after
identity filtering. It performs no network call, model inference, database
write, migration, or private dictionary lookup. New source platforms should
continue to adapt into the standard evidence layer; they do not need a new
named-collection implementation.
