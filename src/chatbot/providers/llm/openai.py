"""OpenAI chat adapter (Chat Completions API). Thin on purpose: swap or extend freely."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from chatbot.core.registry import registry
from chatbot.core.settings import ConfigError, LLMProfile
from chatbot.core.types import Completion, Delta, Message, Usage


class OpenAILLM:
    def __init__(self, profile: LLMProfile, client: Any | None = None) -> None:
        self.model_id = profile.model
        self.params = dict(profile.params)
        if client is None:
            from openai import AsyncOpenAI

            env = profile.api_key_env or "OPENAI_API_KEY"
            api_key = os.environ.get(env, "").strip()
            if not api_key:  # the SDK would raise with a long traceback
                raise ConfigError(
                    f"Set {env} for the OpenAI model {profile.model} (see .env.example), or "
                    "run CHATBOT_PROFILE=offline, which needs no key."
                )
            client = AsyncOpenAI(api_key=api_key, base_url=profile.base_url)
        self.client = client

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    async def generate(
        self, messages: list[Message], *, schema: type[BaseModel] | None = None, **opts: Any
    ) -> Completion:
        opts.pop("purpose", None)
        kwargs: dict[str, Any] = {**self.params, **opts}
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": False,
                },
            }
        resp = await self.client.chat.completions.create(
            model=self.model_id, messages=self._messages(messages), **kwargs
        )
        usage = Usage()
        if resp.usage is not None:
            usage = Usage(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
            )
        return Completion(
            text=resp.choices[0].message.content or "", usage=usage, model=self.model_id
        )

    async def stream(self, messages: list[Message], **opts: Any) -> AsyncIterator[Delta]:
        opts.pop("purpose", None)
        kwargs: dict[str, Any] = {**self.params, **opts}
        resp = await self.client.chat.completions.create(
            model=self.model_id,
            messages=self._messages(messages),
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        async for event in resp:
            if event.choices:
                text = event.choices[0].delta.content or ""
                if text:
                    yield Delta(text=text)
            if getattr(event, "usage", None):
                yield Delta(
                    usage=Usage(
                        prompt_tokens=event.usage.prompt_tokens,
                        completion_tokens=event.usage.completion_tokens,
                    )
                )


@registry.register("llm", "openai")
def make_openai_llm(profile: LLMProfile) -> OpenAILLM:
    return OpenAILLM(profile)
