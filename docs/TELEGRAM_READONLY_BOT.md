# Telegram read-only bot

M15 provides a text-only phone loop for exercising Echo Pact against the
read-only ChatGPT + Codex generation used for real-life recall testing. It
intentionally adds no voice recognition, proactive messaging, Web UI, or
database write route.

## Boundary

The launcher starts one loopback-only HTTP API on `127.0.0.1`, then long-polls
Telegram from the same process:

```text
Telegram Bot API
  -> whitelist/private-chat check
  -> 127.0.0.1 read-only debug API
  -> SQLite mode=ro + PRAGMA query_only
  -> optional OpenAI-compatible answer model
  -> Telegram reply
```

Before starting, the API inspects the database through the read-only
connector. The real-life acceptance phase refuses to start unless the database
contains exactly the approved `chatgpt` and `codex_local` source channels. A
Codex-only, GPT-only, or unclassified database therefore cannot accidentally
stand in for the intended two-source test. The raw records keep their distinct
source kinds and visibility metadata; only one bounded recall reads across
both. Each process also creates an ephemeral high-entropy token for the
loopback recall route; it is kept only in memory and is never written to
configuration or logs.

Only the configured Telegram user ID is accepted, and only when the message
comes from that user's private chat. Unauthorized or group messages are
silently ignored. Tokens, keys, database paths, request bodies and recalled
text are not logged by the launcher.

## Answers and model failure

The optional model receives only the current question and a separately bounded
subset of Top-K evidence, never the database or full debug packet. The private
recall/debug window and external model evidence window are configured
independently: the real-life rollout may inspect up to 10 records locally while
sending at most 5 excerpts to the model provider. It must answer from that
evidence and preserve uncertainty. This still transmits the bounded question
and excerpts to the configured provider, so enabling a model is an explicit
private configuration choice.

When the model is missing or fails, the bot does not hide the failure. It
returns compact A/B evidence followed by `但嘴被捂住了`. A local recall failure
is reported separately and never converted into an invented answer.

For providers that expose thinking control, `ECHO_LLM_THINKING_MODE` can be
`enabled` or `disabled`; leaving it empty omits the provider-specific field.
`ECHO_LLM_MAX_TOKENS` defaults to 600 and is capped at 4096. The private
DeepSeek rollout uses the current `deepseek-flash` model with thinking disabled
for short, low-cost phone answers.

Every ordinary reply ends with a compact local diagnostic footer: source
channel counts, whether M8 candidate expansion ran, and how many bounded
records were hidden by Top-K or exact-mirror compaction. This keeps everyday
phone testing inspectable without copying the full CLI panel into Telegram.

## Inspect the previous answer

After any answered question, send:

```text
/why
```

The bot renders the already-captured previous recall; it does not recall again
and it does not call the model again. The phone panel shows the question, model
status, M8 candidates, actual recall passes, GPT/Codex source labels, evidence
basis, rank metadata, exact-mirror compaction, Top-K exclusions and the record
text. Each result is explicitly labelled either `脑袋看到` or `未送脑袋`,
using the configured external-model evidence limit. This is the boundary needed
to distinguish a retrieval miss from a model synthesis error.

For privacy and fidelity, the panel displays at most the same 1200 characters
per record that the model could receive. A longer record is visibly marked as
truncated; `/badcase` still preserves the bounded recall packet. Long panels are
split into multiple Telegram messages below the platform message limit. Like
ordinary replies, this deliberately sends private recalled text through the
configured private Telegram chat and remains protected by the user-ID and
private-chat whitelist.

## Bad cases

After any answered question, send:

```text
/badcase
```

An optional note may follow the command. The bot writes one atomic JSON packet
containing the last user question, bot answer, model status, model evidence
limit, bounded recalled records, source provenance, M8 trace and the M14 debug
structure. The configured directory must be outside the Git repository; the
packet never contains bot or model credentials. The Echo database remains
byte-for-byte unchanged.

## Configuration and start

Configure the `ECHO_TELEGRAM_*` values shown in `.env.example` in a protected
local environment. `ECHO_LLM_*` is optional and must be configured as a complete
set. The launcher never auto-loads `.env`, which avoids silently picking up an
unreviewed secret file.

```powershell
python scripts/echo_pact_telegram_bot.py
```

The process binds no public listener. Stopping it ends both long polling and
the loopback API. The first real rollout requires a dedicated Bot token and the
allowed private Telegram user ID; do not reuse a token already consumed by a
different long-polling service.

`ECHO_TELEGRAM_RESULT_LIMIT` controls the private recall/debug window and is
bounded to 1..10. `ECHO_LLM_MAX_EVIDENCE_ITEMS` separately controls how many of
those records may be included in the external model request and is also bounded
to 1..10; the reviewed private rollout keeps it at 5.

For the always-on private host, the hardened systemd template lives at
`deploy/cloud/echo-pact-telegram.service.example`. Its environment file is a
root-only deployment secret and must point at one verified immutable snapshot.
The service can write only its separate bad-case directory; the Echo snapshot
and code trees remain read-only.
