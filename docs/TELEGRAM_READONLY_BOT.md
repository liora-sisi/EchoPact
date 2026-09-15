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

The optional model receives only the current question and bounded Top-K
evidence, never the database or full debug packet. It must answer from that
evidence and preserve uncertainty. This still transmits the bounded question
and excerpts to the configured provider, so enabling a model is an explicit
private configuration choice.

When the model is missing or fails, the bot does not hide the failure. It
returns compact A/B evidence followed by `但嘴被捂住了`. A local recall failure
is reported separately and never converted into an invented answer.

Every ordinary reply ends with a compact local diagnostic footer: source
channel counts, whether M8 candidate expansion ran, and how many bounded
records were hidden by Top-K or exact-mirror compaction. This keeps everyday
phone testing inspectable without copying the full CLI panel into Telegram.

## Bad cases

After any answered question, send:

```text
/badcase
```

An optional note may follow the command. The bot writes one atomic JSON packet
containing the last user question, bot answer, model status, bounded recalled
records, source provenance, M8 trace and the M14 debug structure. The configured
directory must be outside the Git repository; the packet never contains bot or
model credentials. The Echo database remains byte-for-byte unchanged.

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
