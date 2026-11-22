"""LangGraph as the orchestrator (ADR 029). Optional: ``uv sync --extra langgraph`` and
``orchestrator: langgraph``.

The graph runs the same steps as the plain-Python orchestrator, as nodes::

    START -> check_input -> retrieve -> answer -> check_grounded -> check_output -> END
                 |              |          |            |   |
                 v              v          v            v   v
                END          clarify <-----+------------+  END (refused)
                                |
                                v
                               END

Each node reads and updates the ``TurnState`` in the graph state and names the next node in
``state.route``; conditional edges follow it. Delta events go out through LangGraph's
custom stream (``get_stream_writer``), so streaming, the SSE contract (ADR 014), guards,
memory, tracing and cost accounting all behave exactly as with ``orchestrator: simple``.

Start here when a turn needs more than one pass: add a tool node and an edge back to
``answer``, a human-approval step with ``interrupt()``, or a checkpointer (``build_graph``
returns the uncompiled graph; ``compile(checkpointer=...)`` it in your subclass).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypedDict

from chatbot.core.registry import registry
from chatbot.core.settings import Settings
from chatbot.core.types import ChatEvent, ChatRequest
from chatbot.orchestration.simple import SimpleOrchestrator
from chatbot.orchestration.state import TurnState

END_ROUTE = "finish"


class GraphState(TypedDict):
    turn: TurnState


class LangGraphOrchestrator(SimpleOrchestrator):
    def __init__(self, settings: Settings, **parts: Any) -> None:
        super().__init__(settings, **parts)
        self.graph = self.build_graph().compile()

    def build_graph(self) -> Any:
        try:
            from langgraph.config import get_stream_writer
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:
            raise RuntimeError(
                "orchestrator: langgraph needs the langgraph extra: uv sync --extra langgraph"
            ) from exc

        def step(fn: Callable[[TurnState], Awaitable[None]]) -> Any:
            async def node(state: GraphState) -> GraphState:
                await fn(state["turn"])
                return {"turn": state["turn"]}

            node.__name__ = fn.__name__
            return node

        def streaming(fn: Callable[[TurnState], AsyncIterator[ChatEvent]]) -> Any:
            async def node(state: GraphState) -> GraphState:
                write = get_stream_writer()
                async for event in fn(state["turn"]):
                    write(event)
                return {"turn": state["turn"]}

            node.__name__ = fn.__name__
            return node

        def route(state: GraphState) -> str:
            return state["turn"].route

        graph = StateGraph(GraphState)
        graph.add_node("check_input", step(self.check_input))
        graph.add_node("retrieve", step(self.retrieve))
        graph.add_node("answer", streaming(self.answer))
        graph.add_node("check_grounded", step(self.check_grounded))
        graph.add_node("check_output", step(self.check_output))
        graph.add_node("clarify", streaming(self.clarify))

        graph.add_edge(START, "check_input")
        graph.add_conditional_edges("check_input", route, {"retrieve": "retrieve", END_ROUTE: END})
        graph.add_conditional_edges("retrieve", route, {"answer": "answer", "clarify": "clarify"})
        graph.add_conditional_edges(
            "answer",
            route,
            {
                "check_grounded": "check_grounded",
                "check_output": "check_output",
                "clarify": "clarify",
            },
        )
        graph.add_conditional_edges(
            "check_grounded",
            route,
            {"check_output": "check_output", "clarify": "clarify", END_ROUTE: END},
        )
        graph.add_edge("check_output", END)
        graph.add_edge("clarify", END)
        return graph

    async def _flow(self, req: ChatRequest, draft: bool) -> AsyncIterator[ChatEvent]:
        final: GraphState | None = None
        async for mode, chunk in self.graph.astream(
            {"turn": TurnState(request=req, draft=draft)}, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                yield chunk
            else:
                final = chunk
        if final is None:
            raise RuntimeError("the graph produced no state")
        yield self.terminal(final["turn"])


@registry.register("orchestrator", "langgraph")
def make_langgraph(settings: Settings, **parts: Any) -> LangGraphOrchestrator:
    return LangGraphOrchestrator(settings, **parts)
