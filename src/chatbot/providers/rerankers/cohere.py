"""Cohere Rerank over plain HTTP (no SDK): POST {base_url}/v2/rerank.

Set COHERE_API_KEY (or the profile's api_key_env). Cohere reports what it bills in each
response (``meta.billed_units``: search units or input tokens). That goes on the call's span
(``rerank <model>``) with the number of attempts, and is priced with the profile's
``price_per_ksearch`` or ``price_per_mtok.input`` into ``usage.cost_usd`` and the usage record.
Cohere publishes no rerank prices on its pricing page; take them from your dashboard.

Retries: 429, 5xx, timeouts and connection errors are retried, other 4xx are not. A chat turn
retries twice within about 1.5 s and then lets the pipeline fall back to the retrieval order.
Under ``patient_reranking()`` (calibrate, eval) it backs off for up to about 3 minutes, which
outlasts a trial key's 10 calls per minute. A ``Retry-After`` header is respected.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from chatbot.core.registry import registry
from chatbot.core.settings import RerankerProfile
from chatbot.core.types import ScoredChunk, Usage
from chatbot.observability.tracing import SpanHandle, count, span
from chatbot.observability.usage import UsageCall, record_call
from chatbot.providers.rerankers import RerankError, apply_scores, is_patient, rerank_cost

DEFAULT_BASE_URL = "https://api.cohere.com"
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
QUICK_DELAYS: tuple[float, ...] = (0.5, 1.0)  # chat turns: someone is waiting
PATIENT_DELAYS: tuple[float, ...] = (2, 4, 8, 15, 30, 30, 30, 30, 30)  # ~3 min in all
MAX_RETRY_AFTER_S = 60.0


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return min(float(resp.headers["retry-after"]), MAX_RETRY_AFTER_S)
    except (KeyError, ValueError):
        return None


class CohereReranker:
    def __init__(
        self,
        profile: RerankerProfile,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.model = profile.model
        self.signature = f"rerank[{profile.model}]"
        self.base_url = (profile.base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key_env = profile.api_key_env or "COHERE_API_KEY"
        self.params = dict(profile.params)
        self.price_per_ksearch = profile.price_per_ksearch
        self.price_per_mtok = dict(profile.price_per_mtok)
        self.client = client
        self.sleep = sleep

    async def _post(
        self, client: httpx.AsyncClient, body: dict[str, Any], key: str, s: SpanHandle
    ) -> Any:
        delays: Sequence[float] = PATIENT_DELAYS if is_patient() else QUICK_DELAYS
        problem = ""
        status: int | None = None
        for attempt in range(len(delays) + 1):
            s.set({"chatbot.rerank.attempts": attempt + 1})
            if attempt:
                count("chatbot.rerank.retries", 1, {"chatbot.rerank.reason": problem})
            try:
                resp = await client.post(
                    f"{self.base_url}/v2/rerank",
                    json=body,
                    headers={"Authorization": f"Bearer {key}"},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                problem, status = type(exc).__name__, None
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code not in RETRY_STATUS:
                    raise RerankError(
                        f"Cohere rerank failed: HTTP {resp.status_code} {resp.text[:200]}",
                        resp.status_code,
                    )
                problem = f"HTTP {resp.status_code}"
                status = resp.status_code
                if attempt < len(delays):
                    wait = _retry_after(resp)
                    if wait is not None and (is_patient() or wait <= delays[attempt]):
                        await self.sleep(max(wait, delays[attempt]))
                        continue
            if attempt < len(delays):
                await self.sleep(delays[attempt])
        raise RerankError(
            f"Cohere rerank failed after {len(delays) + 1} attempts: {problem}", status
        )

    async def rerank(
        self, query: str, hits: list[ScoredChunk], top_n: int
    ) -> tuple[list[ScoredChunk], Usage]:
        if not hits:
            return [], Usage()
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RerankError(f"Set {self.api_key_env} to use the Cohere reranker", permanent=True)
        body: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": [h.chunk.text for h in hits],
            **self.params,
        }
        client = self.client or httpx.AsyncClient(timeout=30)
        attrs = {"gen_ai.request.model": self.model, "chatbot.rerank.documents": len(hits)}
        started = time.perf_counter()
        try:
            with span(f"rerank {self.model}", attrs=attrs) as s:
                data = await self._post(client, body, key, s)
                billed = (data.get("meta") or {}).get("billed_units") or {}
                cost = rerank_cost(billed, self.price_per_ksearch, self.price_per_mtok)
                s.set({f"chatbot.rerank.billed.{k}": v for k, v in billed.items()})
                s.set({"chatbot.rerank.cost_usd": cost})
        finally:
            if self.client is None:
                await client.aclose()
        record_call(
            UsageCall(
                kind="rerank",
                model=self.model,
                role="rerank",
                purpose="rerank",
                cost_usd=cost,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        )
        scores = [0.0] * len(hits)
        for item in data.get("results", []):
            scores[int(item["index"])] = float(item["relevance_score"])
        return apply_scores(hits, scores, top_n), Usage(cost_usd=cost)


@registry.register("reranker", "cohere")
def make_cohere(profile: RerankerProfile) -> CohereReranker:
    return CohereReranker(profile)
