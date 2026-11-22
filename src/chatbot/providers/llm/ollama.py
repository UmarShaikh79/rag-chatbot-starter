"""Ollama chat adapter (native ``/api/chat``). Local models, no API key (ADR 009).

Profile ``params`` (config/models.yaml), all optional:

* ``options``: passed to Ollama as-is, e.g. ``{temperature: 0.2, num_ctx: 8192}``.
  The default context window of many models is small; RAG prompts with six chunks need
  ``num_ctx`` of 8192 or more.
* ``keep_alive``: how long the model stays loaded after a call, e.g. ``"10m"`` or ``-1``.
* ``think``: ``false`` turns off the reasoning phase of thinking models (Qwen 3.x,
  DeepSeek-R1, gpt-oss). Any ``<think>...</think>`` block that still reaches the text is
  removed, so reasoning never shows up in an answer or a citation check.
* ``timeout_s``: per-request timeout (default 300; CPU-only machines are slow).

Structured output (``schema=``) uses Ollama's ``format`` with the JSON schema.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import BaseModel

from chatbot.core.registry import registry
from chatbot.core.settings import LLMProfile
from chatbot.core.types import Completion, Delta, Message, Usage
from chatbot.providers.ollama_client import OllamaError, make_client, raise_for_error

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)

# Generic option names the rest of the code may pass, mapped to Ollama's names.
_OPTION_ALIASES = {"max_tokens": "num_predict", "temperature": "temperature", "seed": "seed"}


def strip_thinking(text: str) -> str:
    return _THINK.sub("", text)


class OllamaLLM:
    def __init__(self, profile: LLMProfile, client: httpx.AsyncClient | None = None) -> None:
        self.model_id = profile.model
        params = dict(profile.params)
        self.options: dict[str, Any] = dict(params.pop("options", {}) or {})
        self.keep_alive = params.pop("keep_alive", None)
        self.think = params.pop("think", None)
        timeout = float(params.pop("timeout_s", 300))
        if params:
            raise ValueError(
                f"Unknown params for Ollama model {profile.model}: {sorted(params)} "
                "(allowed: options, keep_alive, think, timeout_s)"
            )
        self.client = client or make_client(profile.base_url, profile.api_key_env, timeout)

    def _payload(self, messages: list[Message], stream: bool, opts: dict[str, Any]) -> dict:
        opts.pop("purpose", None)
        options = dict(self.options)
        for key, value in opts.items():
            if key not in _OPTION_ALIASES:
                raise ValueError(f"Ollama adapter does not support the option {key!r}")
            options[_OPTION_ALIASES[key]] = value
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
        }
        if options:
            payload["options"] = options
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        if self.think is not None:
            payload["think"] = self.think
        return payload

    @staticmethod
    def _usage(data: dict[str, Any]) -> Usage:
        return Usage(
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
        )

    async def generate(
        self, messages: list[Message], *, schema: type[BaseModel] | None = None, **opts: Any
    ) -> Completion:
        payload = self._payload(messages, False, opts)
        if schema is not None:
            payload["format"] = schema.model_json_schema()
        try:
            resp = await self.client.post("/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {self.client.base_url}: {exc}. Start it or set "
                "OLLAMA_BASE_URL."
            ) from exc
        raise_for_error(resp, self.model_id)
        data = resp.json()
        text = strip_thinking((data.get("message") or {}).get("content") or "")
        return Completion(text=text.strip(), usage=self._usage(data), model=self.model_id)

    async def stream(self, messages: list[Message], **opts: Any) -> AsyncIterator[Delta]:
        payload = self._payload(messages, True, opts)
        thinking = _ThinkFilter()
        try:
            async with self.client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise_for_error(resp, self.model_id, body)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise OllamaError(f"Ollama error for {self.model_id}: {data['error']}")
                    text = (data.get("message") or {}).get("content") or ""
                    out = thinking.feed(text)
                    if out:
                        yield Delta(text=out)
                    if data.get("done"):
                        yield Delta(usage=self._usage(data))
        except httpx.ConnectError as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {self.client.base_url}: {exc}. Start it or set "
                "OLLAMA_BASE_URL."
            ) from exc


class _ThinkFilter:
    """Drops a ``<think>...</think>`` block that some models stream before their answer.

    The block can span many stream pieces, so the filter remembers whether it is inside one.
    """

    def __init__(self) -> None:
        self.inside = False

    def feed(self, text: str) -> str:
        """The part of this piece to show."""
        before = ""
        if "<think>" in text:
            before, _, text = text.partition("<think>")
            self.inside = True
        if self.inside:
            if "</think>" not in text:
                return before  # still thinking: show nothing more
            text = text.partition("</think>")[2].lstrip()
            self.inside = False
        return before + text


@registry.register("llm", "ollama")
def make_ollama_llm(profile: LLMProfile) -> OllamaLLM:
    return OllamaLLM(profile)
