# ADR 028: Token and cost accounting with a usage record per operation

Status: Accepted (milestone 5)

## Decision

Every model and embedding call goes through a wrapper (ADR 026) that prices it with the
profile's `price_per_mtok` and appends a `UsageCall` (model, role, purpose, tokens, cost,
duration) to the ledger of the current operation. An operation is a chat turn, a background
job, or a CLI run such as `chatbot ingest --inline`; nested work (inline memory refresh,
HyDE, the groundedness judge, the injection classifier) lands in the same ledger.

At the end of the operation one row goes into `usage_records` (migration 0005): totals,
`unpriced_calls`, the per-call breakdown as JSON, the trace ID, and the caller's `user_id`
and `conversation_id`. No message text. `GET /v1/usage` (admin) and `chatbot usage` sum it by
day, user, model or operation. Writing a record never fails the operation.

The response's `usage` field is unchanged; the record is the same total, with the breakdown.
`observability.usage.persist: false` turns records off (metrics still flow). Eval runs write
none.

`forget_user` keeps the totals but sets `user_id` and `conversation_id` to NULL on the user's
rows (ADR 015): the spend stays accountable without knowing whose it was.

## Alternatives considered

- Metrics only (`chatbot.cost`, `gen_ai.client.token.usage`): good for dashboards, but no
  per-user or per-conversation answer, and gone with the retention window.
- A row per call: precise but many rows per turn; the JSON breakdown answers the same
  questions.
- Deleting usage rows on `forget_user`: removes spend from invoices and budgets.

## Consequences

- Cost is only as good as `price_per_mtok`, which ships `null`: until you fill it from your
  provider's pricing page, reports show tokens and count calls as unpriced.
- A stream the client abandons is recorded with the calls that finished; the answer call
  that was cut off is not, since providers report usage at the end of a stream.
- Cached-input and reasoning-token prices are not modelled; add fields to `Usage` and
  `priced` if your provider bills them separately.
