# Memory

What the bot remembers, and how to make it forget. Within a conversation it keeps the
recent turns and a rolling **summary** of older ones; across conversations it keeps short
**facts** about a user ("The user is on the Team plan"). Everything is redacted before it's
stored, and one call deletes everything about a user.

Back to the [docs home](../README.md) · See also: [HTTP API: memory](../api.md#memory)

## At a glance

| | |
| --- | --- |
| **What it does** | Saves each turn (redacted); loads the last `history_turns` turns, the conversation summary and the user's facts into the answer prompt; refreshes summaries and facts after each turn; lists and deletes facts; forgets a user. |
| **Turned on by** | On by default; `memory.*` in [`config/app.yaml`](../../config/app.yaml). Facts need a `user_id` (or a JWT). |
| **Main files** | [`memory/postgres.py`](../../src/chatbot/memory/postgres.py) (`PostgresMemoryStore`), [`memory/manager.py`](../../src/chatbot/memory/manager.py) (`MemoryManager`, `format_memory`), [`memory/facts.py`](../../src/chatbot/memory/facts.py) (`parse_facts`, `fact_key`), [`api/routes/memory.py`](../../src/chatbot/api/routes/memory.py), the `MemoryStore` Protocol in [`core/interfaces.py`](../../src/chatbot/core/interfaces.py) |
| **Depends on** | Postgres; the `cheap` model (summaries, facts); the [worker](background-jobs.md) when `run_in: worker` (the default); [guardrails](guardrails.md) (redaction, screening facts) |
| **Used by** | [Orchestration](orchestration.md) (`check_input` loads, `_finish` saves), the answer and clarify [prompts](prompts.md) |
| **Decisions** | ADR [008](../adr/008-memory-keyed-by-caller-user-id.md) (memory keyed by the caller's user ID), ADR [015](../adr/015-redact-before-persisting-delete-everything-per-user.md) (redact before storing, delete everything per user), ADR [016](../adr/016-rolling-summaries-and-long-term-facts.md) (rolling summaries and long-term facts) |

## What it does

| Kind | Scope | Kept | Reaches the model as |
| --- | --- | --- | --- |
| **Turns** | One conversation | Every question and answer, redacted | The last `history_turns` (10) under "Conversation so far" |
| **Summary** | One conversation | One text of up to 1,500 characters | "Summary of earlier conversation" in the `memory` section |
| **Facts** | One `user_id`, every conversation | Up to 50 short sentences | "What the user told you in past conversations (may be outdated)" |

The history lets follow-ups work ("and for monthly plans?"). The summary keeps long
conversations coherent without sending every turn. Facts let the bot tailor answers ("you're on
the Team plan, so…"); the answer prompt says to use memory for context only, **never as a
source to cite**.

**Who owns what** (ADR 008): a conversation belongs to the `user_id` that started it; loading
it as another user is a 403. With API keys, your backend passes the `user_id`; with a JWT, it's
the token's subject and can't be chosen. Without a `user_id`, a conversation still has history
and a summary, but no facts.

Real example (offline profile, a JWT for `alice`): after asking "I am on the Team plan. How
long is the refund window for annual plans?", `GET /v1/users/alice/memory` returned:

```json
{"user_id": "alice", "facts": [{"id": 5, "fact": "The user said: I am on the Team plan.",
  "source_conversation_id": "d0e7cfdc87ee486ea03c8c74f06d6480",
  "created_at": "2026-11-23T01:03:08.750910Z", "updated_at": "2026-11-23T01:03:08.750910Z"}]}
```

(The offline fake model extracts facts by rule, hence "The user said: …"; a real model writes
"The user is on the Team plan.") Then `DELETE /v1/users/alice/memory` returned:

```json
{"user_id": "alice", "deleted": {"user_facts": 1, "conversation_summaries": 2, "turns": 4,
  "conversations": 2, "jobs": 0, "documents": 0, "usage_records_anonymised": 2}}
```

## How it works

**Loading** (`check_input` in [`orchestration/simple.py`](../../src/chatbot/orchestration/simple.py)):
`memory.load(user_id, conversation_id, history_turns, facts_limit)`
([`PostgresMemoryStore.load`](../../src/chatbot/memory/postgres.py)):

- no `conversation_id`: a new ID, no history, the user's facts;
- an unknown ID: used as given, as a new conversation;
- a known ID owned by someone else: `ForbiddenError` (403);
- otherwise: the last `history_turns` turns (oldest first), the summary, the facts.

**Saving** (`_finish` → `_persist`): with `redact_before_persist` (default), the question goes
through the input redactor and the answer through the output redactor, so what's stored
matches what the user saw. `save_turns` writes both; then `memory_manager.after_turn(ctx)`
(not after refusals).

**Refreshing** ([`MemoryManager`](../../src/chatbot/memory/manager.py)):

1. `after_turn`: with `run_in: worker`, enqueue a `memory_refresh` job (deduplicated per
   conversation, so a burst of turns makes one job); with `inline`, run `refresh` now, and
   never fail the turn if it errors.
2. `refresh(conversation_id)` (the worker's `memory_refresh` handler calls it):
   - **Summary:** turns that have left the history window and aren't summarized yet; once there
     are at least `summary.refresh_after_turns` (4), `_summarize` folds them into the summary
     with `summarize/v1`, redacts the result and caps it at `max_chars`.
   - **Facts** (only with a `user_id`): `_extract` sends the new turns and the known facts to
     `extract_facts/v1` (JSON: `{"facts": [...]}`). A fact is dropped if it's longer than
     `max_fact_chars`, contains PII or a redaction placeholder, trips the context injection
     check, or is already known (`fact_key`: same text ignoring case, accents and
     punctuation). `add_facts` stores the rest and keeps the newest `max_facts_per_user`.
   - Cursors (`summarized_until`, `facts_until`) record what's been processed, so a refresh
     never reads a turn twice. A conversation deleted before its job ran is skipped.

**Into the prompt:** `format_memory(ctx)` builds the `memory` variable of `answer/v2` from the
summary and facts; `_history(ctx)` builds `history`.

**Forgetting** (`DELETE /v1/users/{user_id}/memory`): first vectors outside Postgres (Qdrant),
then one transaction deletes the user's facts, summaries, turns, conversations, queued jobs
and owned documents, and unlinks their usage records (totals stay for accounting).

## Inputs and outputs

**In:** `user_id` and `conversation_id` from the chat request (`user_id` from the token for end
users).

**Out:** a `ConversationContext` (`conversation_id`, `user_id`, `history`, `summary`, `facts`,
`is_new`) for the turn; `UserFact`s (`id`, `fact`, `source_conversation_id`, `created_at`,
`updated_at`) from the API; a `DeletionReport` (counts per table) from forgetting.

| Route | Does |
| --- | --- |
| `GET /v1/users/{user_id}/memory` | The user's facts, most recently seen first. |
| `DELETE /v1/users/{user_id}/memory/facts/{id}` | Forget one fact (204; 404 if not theirs). |
| `DELETE /v1/users/{user_id}/memory` | Forget everything about the user. |

End users (JWT without the admin scope) may only call these for themselves.

## Turn it on, change it, turn it off

Settings ([configuration: `memory`](../configuration.md#memory)):

| Setting | Default | Notes |
| --- | --- | --- |
| `memory.history_turns` | 10 | Turns loaded into the prompt. |
| `memory.redact_before_persist` | true | Redact every stored turn, even with input redaction off. |
| `memory.run_in` | `worker` | `worker` needs `chatbot worker` running; `inline` adds the model calls to the turn. |
| `memory.summary.enabled` / `refresh_after_turns` / `max_chars` / `prompt` | true / 4 / 1500 / `summarize/v1` | |
| `memory.long_term.enabled` | true | |
| `memory.long_term.max_facts_per_user` / `facts_in_prompt` / `max_fact_chars` | 50 / 20 / 200 | |
| `memory.long_term.prompt` | `extract_facts/v1` | |

**Turning parts off:** `summary.enabled: false` and/or `long_term.enabled: false`; with both
off, no refresh jobs run. History can't be turned off (follow-ups need it), but
`history_turns: 1` keeps it minimal. To keep memory but hide it from the model, use
`answer.prompt: answer/v1` (no memory section).

**Retention:** nothing expires on its own. Delete per user through the API, or add a scheduled
SQL job that deletes `turns` and `conversations` older than your retention period.

`memory.backend` exists in the settings but isn't read: the container always builds
`PostgresMemoryStore`. Swap the store as described below.

## Extend it

- **Another store** (Redis for history, a vendor's memory service): implement the
  `MemoryStore` Protocol (`load`, `save_turns`, `memory_state`, `turns_after`, `save_summary`,
  `add_facts`, `list_facts`, `delete_fact`, `forget_user`) and pass it to the container:
  `container.build(settings, ..., memory=MyStore())`. Keep `forget_user` deleting everything
  it holds, and keep the owner check in `load`.
- **Another memory policy** (a memory service such as Mem0 or Zep): replace `MemoryManager`
  with a class that has `after_turn`, `refresh` and `facts_in_prompt`, and pass
  `memory_manager=...`.
- **Different summaries or facts:** new versions of `summarize` and `extract_facts`
  ([prompts](prompts.md)).

More in [extending: change how memory works](../extending.md#change-how-memory-works).

## Test it

```bash
uv run pytest tests/unit/test_memory.py
make test-db     # tests/integration/test_jobs_memory.py against Postgres
```

| Test file | Covers |
| --- | --- |
| [`test_memory.py`](../../tests/unit/test_memory.py) | old turns fold into the summary once enough have left the window; summary and facts reach the answer prompt; facts need a `user_id` and are deduplicated; facts with PII or injection are dropped; the fact limit keeps the newest; switching memory off; one job per conversation in worker mode; refresh after forgetting is a no-op; forgetting removes everything |
| [`test_jobs_memory.py`](../../tests/integration/test_jobs_memory.py) | history and forgetting in Postgres; the history window is the most recent turns; summaries and facts round-trip |
| [`test_orchestrator.py`](../../tests/unit/test_orchestrator.py) | PII redacted before it's stored; history per conversation with the owner check |
| [`test_auth.py`](../../tests/unit/test_auth.py) | end users manage only their own memory |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| Facts and summaries never appear | No worker running (`run_in: worker`) | Start `chatbot worker` (Compose runs one), or `run_in: inline`. |
| No facts for a user | Requests have no `user_id` | Pass `user_id` (API key) or use JWTs. |
| 403 "This conversation belongs to a different user" | Reusing another user's `conversation_id` | Start a new conversation for that user. |
| The bot "remembers" something wrong | An extracted fact is wrong or outdated | `DELETE /v1/users/{id}/memory/facts/{fact_id}`; the prompt already marks facts as possibly outdated. |
| Stored turns contain `[EMAIL]` | Redaction before storing, by design | Expected (ADR 015). |
| Follow-ups lose context in long chats | `history_turns` too small, or summaries off | Raise `history_turns`; keep `summary.enabled`; consider [query rewrite](retrieval-strategies.md). |

## Related

- Where memory is loaded and saved: [orchestration](orchestration.md).
- What gets redacted: [guardrails](guardrails.md).
- Who may read whose memory: [authentication](authentication.md).
- The refresh jobs: [background jobs](background-jobs.md).
- Terms: [PII](../glossary.md#pii), [job](../glossary.md#job), [worker](../glossary.md#worker).
