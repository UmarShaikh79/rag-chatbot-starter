# ADR 029: LangGraph as an optional orchestrator adapter

Status: Accepted (milestone 5); keeps ADR 003

## Decision

The plain-Python orchestrator stays the default (ADR 003). Its turn is now a sequence of
step methods (`check_input`, `retrieve`, `answer`, `clarify`, `check_grounded`,
`check_output`) that read and update one `TurnState` and name the next step in
`state.route`. `orchestrator: langgraph` (`uv sync --extra langgraph`) runs the same methods
as nodes of a LangGraph `StateGraph`, with conditional edges that follow `route`; delta
events go out through LangGraph's custom stream. Orchestrators are chosen by name from the
registry, so `orchestrator: yours` works the same way.

Because both run the same steps, guards, memory, citations, the SSE contract (ADR 014),
tracing and accounting are identical. A test runs every scenario (answer, clarify, refusal,
replace, abstain, too long, groundedness, buffered) through both and compares the events.

## Alternatives considered

- LangGraph as the core: a framework dependency for every user, for a flow that is linear
  today (ADR 003).
- A separate LangGraph flow written from scratch: two flows that drift apart.
- LangChain's runnables: more abstraction than a linear RAG turn needs.

## Consequences

- The adapter is the place to start for multi-step work: tool calls with an edge back to
  `answer`, human approval with `interrupt()`, or a checkpointer. Subclass
  `LangGraphOrchestrator` and change `build_graph`.
- `TurnState` is a Pydantic model, so a checkpointer can store it; nothing in the template
  uses one, because conversation memory already lives in Postgres.
- LangGraph's own overhead per turn is small next to the model calls, but it was not
  measured here; check it if you care about the last milliseconds.
