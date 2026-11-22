# ADR 016: Rolling summaries and long-term facts

Status: Accepted (milestone 2)

## Decision

Two kinds of memory sit on top of the stored turns, both switchable in `memory.*`:

- **Rolling summary per conversation.** The prompt gets the last `history_turns` turns verbatim. Turns that scroll out of that window are folded into one summary (`summarize/v1` on the `cheap` model) once `refresh_after_turns` of them have piled up, so a long conversation costs one short summary instead of an ever-growing history.
- **Long-term facts per `user_id`.** After each turn, new user turns are read for durable facts ("The user is on the Team plan") with `extract_facts/v1`. Facts are deduplicated by normalised text, capped at `max_facts_per_user` (least recently seen dropped first) and the most recent `facts_in_prompt` go into the answer prompt's memory section. Conversations without a `user_id` get no long-term memory.

Both run in a `memory_refresh` job on the Postgres queue after each turn (`run_in: worker`, deduplicated per conversation) or inline before the response returns (`run_in: inline`). Job payloads carry only IDs. The answer prompt (`answer/v2`) treats memory as context about the user, never as a citable source, and as data rather than instructions.

ADR 015 applies: summaries and facts are made from turns that were redacted before storage, the summary is redacted again, and a fact that still contains PII, a redaction placeholder, or trips the injection check is dropped. `forget_user` deletes `user_facts` and `conversation_summaries` with everything else and reports counts per table.

## Alternatives considered

Vector-indexed episodic memory (retrieve past turns by similarity); a memory framework (Mem0, Zep, LangMem); summarising on every turn.

## Why

A summary plus a short fact list covers what most support and product chatbots need, stays in Postgres, and is easy to read, export and delete. A memory framework can replace `MemoryManager` behind the same calls.

## Consequences

One extra `cheap` model call per user turn for facts (when a `user_id` is present) and one every few turns for summaries. In worker mode memory lags the conversation by one job; a gap of up to `refresh_after_turns - 1` turns can sit between the summary and the history window. Facts are the model's paraphrase and can be wrong; `GET /v1/users/{id}/memory` and `DELETE /v1/users/{id}/memory/facts/{fact_id}` let the app show and correct them.
