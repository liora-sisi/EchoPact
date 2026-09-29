# Local recall debug CLI

M14 adds an opt-in, local-only explanation surface over the same adaptive
records-v1 recall used by the read-only MCP gateway. It does not create a
second retriever and it does not change the normal MCP response schema.

## What the panel shows

- the original query and every bounded M8 speech-rescue candidate when a
  caller-owned policy is loaded;
- each adaptive pass, its query, recall mode, caller-derived plan and bounded
  candidate count;
- Top-K records with source labels including `GPT库（官方数据包）`,
  `GPT库（浏览器抓取）`, `Codex库`, or `其他来源`, evidence confidence and
  its documented basis, lexical/intent rank metadata, and the passes that
  found the record; Codex results also show their stored `commentary` or
  `final_answer` phase and the query's bounded phase preference;
- M12 exact-mirror compaction, including the representative record and the
  number of compacted counterparts;
- candidates omitted by the public support-unit limit and duplicate records
  already retained from an earlier pass.

The records-v1 engine does **not** use the legacy
`semantic / emotion / importance / decay / saga` weighted formula. The panel
states that explicitly and reports the ranking data that the current SQLite
path actually uses. `confidence` remains an evidence-quality heuristic, not a
final ranking weight or probability.

Identity visibility and record-time scope are applied in SQL before ranking.
The panel may state that a filter was applied, but it never enumerates or
reveals an inaccessible record. A target that never enters the bounded
candidate pool is reported as a candidate-generation miss; the panel does not
pretend to know an unstated expected answer.

## Run

Use an existing supported records-v1 database and a fixed active reader:

```powershell
python scripts/recall_debug_cli.py "我最近手腕怎么样" `
  --db D:\private\echo.sqlite3 `
  --agent-id agt-local-reader
```

Omit the positional query to get one interactive prompt. `--json` returns the
same local debug packet that a later bad-case collector can store. Treat JSON
output as private because it contains bounded recalled text.

## Optional private M8 policy

The public repository contains no household correction dictionary. A local
JSON file may be supplied with `--speech-policy`:

```json
{
  "mappings": [
    {"observed": "手完", "intended": "手腕"}
  ],
  "protected_terms": [],
  "max_candidates": 2
}
```

The file is read for that process only. It is not copied into the database,
debug log, repository, or normal MCP configuration.

## Read-only boundary

The CLI always calls the gateway with SQLite `mode=ro` and `PRAGMA query_only`.
It never runs a migration. `bad_cases/` is ignored defensively, but the future
Telegram collector must store real cases in a private directory outside the
repository rather than relying on the ignored repository path.
