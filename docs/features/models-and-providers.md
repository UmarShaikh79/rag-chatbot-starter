# Models and providers

Which language models write the answers and do the small jobs around them, and how to switch
between OpenAI, a local model through Ollama, an OpenAI-compatible server, or the offline fake.
Two **roles**, `answer` and `cheap`, each point at a named profile; changing a model is one line.

Back to the [docs home](../README.md) · See also: [architecture, step 2](../architecture.md#2-the-registry-turns-names-into-parts)

## At a glance

| | |
| --- | --- |
| **What it does** | Sends a list of messages to a chat model and returns its text, either all at once (`generate`) or piece by piece (`stream`), with token counts. |
| **Turned on by** | Always on; `models.answer` and `models.cheap` in [`config/app.yaml`](../../config/app.yaml) name profiles under `llm:` in [`config/models.yaml`](../../config/models.yaml) |
| **Main files** | [`providers/llm/openai.py`](../../src/chatbot/providers/llm/openai.py) (`OpenAILLM`), [`providers/llm/ollama.py`](../../src/chatbot/providers/llm/ollama.py) (`OllamaLLM`), [`providers/llm/fake.py`](../../src/chatbot/providers/llm/fake.py) (`FakeLLM`), [`providers/ollama_client.py`](../../src/chatbot/providers/ollama_client.py), the `LLM` Protocol in [`core/interfaces.py`](../../src/chatbot/core/interfaces.py), `make_llm` and `metered_llm` in [`core/container.py`](../../src/chatbot/core/container.py) |
| **Depends on** | `OPENAI_API_KEY` (OpenAI), a running Ollama with the models pulled (Ollama), or nothing (fake) |
| **Used by** | The [orchestrator](orchestration.md) (answers, clarifying questions), [HyDE and query rewrite](retrieval-strategies.md), memory summaries and facts, the LLM injection check and the groundedness judge |
| **Decisions** | ADR [009](../adr/009-hosted-and-local-defaults.md) (hosted and local defaults), ADR [025](../adr/025-local-models-through-ollamas-native-api.md) (Ollama's native API), ADR [026](../adr/026-tracing-and-metrics-through-a-small-facade-on-opentelemetry.md) and [028](../adr/028-usage-records-per-operation.md) (every call traced and priced) |

## What it does

The bot calls models for two kinds of work, so there are two roles:

| Role | Default (`dev`, `prod`) | Does |
| --- | --- | --- |
| `answer` | `openai:answer` (`gpt-6.2-sol`) | Writes the answer from the sources. Quality matters most here. |
| `cheap` | `openai:cheap` (`gpt-6-luna`) | Clarifying questions, memory summaries and facts; by default also HyDE, query rewrite, the LLM injection check and the groundedness judge. Many small calls, so price and speed matter. |

Each profile picks a **provider**:

| Provider | Profiles | Needs | Notes |
| --- | --- | --- | --- |
| `openai` | `openai:answer`, `openai:cheap` | `OPENAI_API_KEY` | Chat Completions API. With a `base_url`, any OpenAI-compatible server: vLLM, LM Studio, Azure OpenAI's compatible endpoint. |
| `ollama` | `ollama:answer` (`qwen3.5:9b`), `ollama:cheap` (`gemma3:4b`) | Ollama, models pulled | Local; nothing leaves the machine. Profile `local-ollama`. |
| `fake` | `fake:extractive` | Nothing | Not a language model: quotes the source sentence that shares most words with the question, with its `[n]`. For tests, CI and the `offline` profile. |

The model IDs and prices in `config/models.yaml` carry the date they were read from the
provider's pages; check them when you clone.

## How it works

At startup ([`core/container.py`](../../src/chatbot/core/container.py)):

1. `make_llm(settings, role)` reads `settings.llm_profile(role)` (the profile named by
   `models.<role>`) and calls `registry.create("llm", profile.provider, profile)`.
2. `metered_llm(settings, role, llm)` wraps it in `MeteredLLM`
   ([`observability/instrument.py`](../../src/chatbot/observability/instrument.py)): each call
   gets a `chat <model>` span with the role and purpose, `gen_ai.*` token and duration metrics,
   a cost from `price_per_mtok`, and a line in the usage ledger. Providers stay plain adapters.
3. The two wrapped models are passed to everything that needs them (`answer_llm`,
   `cheap_llm`, and `llms = {"answer": ..., "cheap": ...}` for parts with a `model_role` setting).

Per call, the caller renders a [prompt](prompts.md) into messages and calls
`llm.generate(messages, purpose="clarify")` or iterates `llm.stream(messages, purpose="answer")`.
`purpose` labels the span and the usage record; providers drop it before calling the model.

What each provider does:

- **OpenAI** (`OpenAILLM`): `client.chat.completions.create(model, messages, **params)`.
  `generate(..., schema=Model)` asks for JSON matching a Pydantic model (`response_format:
  json_schema`). `stream` sets `stream_options.include_usage` so the last piece carries token
  counts. A missing key stops startup with one line, not a traceback.
- **Ollama** (`OllamaLLM`): `POST /api/chat` with the profile's `options`, `keep_alive` and
  `think`. Streams newline-delimited JSON. Removes any `<think>…</think>` block reasoning
  models emit, also when it spans several stream pieces (`_ThinkFilter`), so reasoning never
  reaches an answer. A model that isn't pulled fails with the `ollama pull` command to run.
- **Fake** (`FakeLLM`): picks a reply by `purpose` from a handler table (`_reply`); answers are
  extractive quotes, clarifying questions a fixed sentence. Tests can pass `script=[...]` to
  force any output.

When a provider fails after its own retries, the API answers **502** with a short message and
the trace ID (a `ProviderError` or an `httpx`/OpenAI error), not 500.

## Inputs and outputs

**In:** a list of `Message(role, content)` (`system`, `user`, `assistant`), plus options
(`purpose`, and anything the provider accepts, such as `temperature`).

**Out:**

- `generate` → `Completion(text, usage, model)`.
- `stream` → `Delta(text)` pieces, the last one carrying `usage`.

`Usage` holds `prompt_tokens`, `completion_tokens`, `embedding_tokens` and `cost_usd`. A real
answer on the offline profile reported `"prompt_tokens": 554, "completion_tokens": 24` (the
fake estimates tokens as characters / 4; real providers report their own counts).

## Turn it on, change it, turn it off

Point a role at another profile ([configuration: models](../configuration.md#models-and-configmodelsyaml)):

```bash
CHATBOT__MODELS__ANSWER=ollama:answer
CHATBOT__MODELS__CHEAP=ollama:cheap
```

or pick a whole profile: `CHATBOT_PROFILE=local-ollama` (Ollama for everything, embeddings
included) or `CHATBOT_PROFILE=offline` (the fake, no network).

A profile's fields:

| Field | Example | Notes |
| --- | --- | --- |
| `provider` | `openai` | `openai`, `ollama`, `fake`, or one you register. |
| `model` | `gpt-6.2-sol` | The provider's model ID. |
| `api_key_env` | `OPENAI_API_KEY` | The variable holding the key. Checked at startup, with every missing key listed in one message. |
| `base_url` | `http://localhost:8001/v1` | Another endpoint for the same API. |
| `params` | `{temperature: 0.2}` | OpenAI: passed to every call. Ollama: `options`, `keep_alive`, `think`, `timeout_s` (anything else is refused). |
| `price_per_mtok` | `{input: 2.00, output: 10.00}` | USD per million tokens, for [usage and cost](../observability.md#cost-and-token-accounting). Without it `cost_usd` is null. |

**A new OpenAI model:** edit `model` (and the prices) in `config/models.yaml`. **Another
OpenAI-compatible server:** add a profile with `provider: openai`, `base_url` and the key
variable its server expects:

```yaml
llm:
  vllm:answer:
    provider: openai
    model: meta-llama/Llama-3.3-70B-Instruct
    base_url: http://vllm:8000/v1
    api_key_env: VLLM_API_KEY
```

**Local with Ollama:** `ollama pull qwen3.5:9b && ollama pull gemma3:4b`, or `make up-ollama`
for Ollama in Docker. RAG prompts with six passages need `num_ctx` of 8192 or more (set in
the profile's `options`); thinking models should run with `think: false`.

**Which parts use `cheap`:** `retrieval.hyde.model_role`, `retrieval.query_rewrite.model_role`,
`guardrails.injection.llm.model_role` and `guardrails.groundedness.model_role` choose `answer`
or `cheap` (default `cheap`). Clarifying questions and memory always use `cheap`. To use one
model for everything, point both roles at the same profile.

**Removing a provider:** delete its profiles. No extra packages to remove: the OpenAI SDK is a
core dependency and Ollama uses `httpx`.

## Extend it

A model is anything with `model_id`, `async generate(messages, *, schema=None, **opts)` and
`stream(messages, **opts)` (an async generator). A minimal adapter for a provider with a
different API:

```python
# src/chatbot/domain/anthropic_llm.py
import os

import httpx

from chatbot.core.registry import registry
from chatbot.core.types import Completion, Delta, Usage


class AnthropicLLM:
    def __init__(self, profile):
        self.model_id = profile.model
        self.params = dict(profile.params)
        self.key = os.environ[profile.api_key_env or "ANTHROPIC_API_KEY"]

    async def generate(self, messages, *, schema=None, **opts):
        opts.pop("purpose", None)  # a label for tracing, not for the provider
        system = "\n".join(m.content for m in messages if m.role == "system")
        chat = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"},
                json={
                    "model": self.model_id,
                    "system": system,
                    "messages": chat,
                    "max_tokens": 1024,
                    **self.params,
                    **opts,
                },
            )
        resp.raise_for_status()  # becomes a 502 with the trace ID
        data = resp.json()
        usage = Usage(
            prompt_tokens=data["usage"]["input_tokens"],
            completion_tokens=data["usage"]["output_tokens"],
        )
        return Completion(text=data["content"][0]["text"], usage=usage, model=self.model_id)

    async def stream(self, messages, **opts):
        # Simplest correct stream: one piece. Replace with the provider's streaming API.
        completion = await self.generate(messages, **opts)
        yield Delta(text=completion.text)
        yield Delta(usage=completion.usage)


@registry.register("llm", "anthropic")
def make_anthropic(profile):
    return AnthropicLLM(profile)
```

Import it from `src/chatbot/domain/__init__.py`, add a profile with `provider: anthropic`, and
point a role at it. Tracing, metrics, prices and the usage ledger apply without more code.
Rules worth keeping:

- Drop `purpose` before calling the provider.
- End `stream` with a `Delta(usage=...)` so streamed turns are counted and priced.
- Raise `chatbot.core.errors.ProviderError` (or let `httpx` errors through) after your retries;
  never put keys or raw upstream bodies in the message, since clients see it.
- Support `schema=` if you'll use the LLM injection classifier or judge with this model; they
  ask for JSON.

More in [extending: add an LLM or embedding provider](../extending.md#add-an-llm-or-embedding-provider).

## Test it

```bash
uv run pytest tests/unit/test_ollama.py tests/unit/test_settings.py
```

| Test file | Covers |
| --- | --- |
| [`test_ollama.py`](../../tests/unit/test_ollama.py) | options and `think` sent, thinking stripped from answers and streams, structured output through `format`, a missing model names the `ollama pull`, an unreachable server is a clear error, unknown params refused |
| [`test_settings.py`](../../tests/unit/test_settings.py) | unknown model names fail at startup; the keys the configured models need; OpenAI clients need a key; one readable message for missing keys |
| [`test_api_errors.py`](../../tests/unit/test_api_errors.py) | provider failures answer 502 with a trace ID |
| [`test_live_openai.py`](../../tests/e2e/test_live_openai.py), [`test_live_ollama.py`](../../tests/e2e/test_live_ollama.py) | the real providers, skipped unless a key or server is configured |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Set OPENAI_API_KEY for the OpenAI model ...` | No key | Add it to `.env`, or `CHATBOT_PROFILE=offline`. |
| `models.answer: '...' is not defined under llm in models.yaml` | A typo, or the profile isn't in `config/models.yaml` | Check the name. |
| 502 "model not found" from OpenAI | The model ID changed or your account lacks it | Update `model` in `config/models.yaml`. |
| Ollama error ending "Pull it first: `ollama pull <model>`" | Not pulled | Run it (with Docker: `make ollama-pull`). |
| Ollama answers ignore later sources | Context window too small | `options: {num_ctx: 8192}`. |
| Local answers are slow; the first is very slow | Model loading, or two models swapping in memory | `keep_alive: 10m`; a smaller cheap model; more memory. |
| `cost_usd` is null | No `price_per_mtok` in the profile | Add prices. |
| Offline answers are quotes, not prose | The fake model | Expected; use a real model. |

## Related

- What gets sent to the model: [prompts](prompts.md).
- How answers are checked and streamed: [answers and streaming](answers-and-streaming.md).
- Embedding models are separate: [embeddings](embeddings.md).
- Costs and traces: [observability](../observability.md).
- Terms: [profile](../glossary.md#profile), [registry](../glossary.md#registry),
  [factory](../glossary.md#factory), [extra](../glossary.md#extra).
