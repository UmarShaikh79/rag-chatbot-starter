"""Ollama adapters against a mocked HTTP server (no Ollama needed).

tests/e2e/test_live_ollama.py runs the same calls against a real server (OLLAMA_TEST_URL).
"""

import json

import httpx
import pytest
from pydantic import BaseModel

from chatbot.core import container
from chatbot.core.settings import EmbeddingProfile, LLMProfile
from chatbot.core.types import Message
from chatbot.providers.embeddings.ollama import OllamaEmbedder
from chatbot.providers.llm.ollama import OllamaLLM, strip_thinking
from chatbot.providers.ollama_client import OllamaError
from tests.conftest import ROOT


class Recorder:
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"path": request.url.path, "body": body, "headers": request.headers})
        return self.handler(request.url.path, body)


def client(handler) -> tuple[httpx.AsyncClient, Recorder]:
    rec = Recorder(handler)
    return (
        httpx.AsyncClient(base_url="http://ollama.test", transport=httpx.MockTransport(rec)),
        rec,
    )


def llm_profile(**params) -> LLMProfile:
    return LLMProfile(provider="ollama", model="qwen3.5:9b", params=params)


MESSAGES = [Message(role="system", content="Be brief."), Message(role="user", content="Hi")]


async def test_generate_sends_options_and_strips_thinking():
    def handler(path, body):
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "<think>hmm</think>\nHello [1]."},
                "prompt_eval_count": 12,
                "eval_count": 4,
                "done": True,
            },
        )

    http, rec = client(handler)
    llm = OllamaLLM(
        llm_profile(think=False, keep_alive="5m", options={"num_ctx": 8192}), client=http
    )
    out = await llm.generate(MESSAGES, purpose="answer", max_tokens=50)
    assert out.text == "Hello [1]." and out.model == "qwen3.5:9b"
    assert out.usage.prompt_tokens == 12 and out.usage.completion_tokens == 4
    body = rec.requests[0]["body"]
    assert rec.requests[0]["path"] == "/api/chat"
    assert body["stream"] is False and body["think"] is False and body["keep_alive"] == "5m"
    assert body["options"] == {"num_ctx": 8192, "num_predict": 50}
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}


async def test_generate_with_schema_uses_format():
    class Verdict(BaseModel):
        ok: bool

    http, rec = client(
        lambda p, b: httpx.Response(200, json={"message": {"content": '{"ok": true}'}})
    )
    out = await OllamaLLM(llm_profile(), client=http).generate(MESSAGES, schema=Verdict)
    assert Verdict.model_validate_json(out.text).ok
    assert rec.requests[0]["body"]["format"]["properties"]["ok"]["type"] == "boolean"


async def test_stream_yields_text_without_think_block_then_usage():
    lines = [
        {"message": {"content": "<think>"}, "done": False},
        {"message": {"content": "secret plan"}, "done": False},
        {"message": {"content": "</think>\n\nThe "}, "done": False},
        {"message": {"content": "answer."}, "done": False},
        {"message": {"content": ""}, "done": True, "prompt_eval_count": 7, "eval_count": 3},
    ]

    def handler(path, body):
        assert body["stream"] is True
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())

    http, _ = client(handler)
    deltas = [d async for d in OllamaLLM(llm_profile(), client=http).stream(MESSAGES)]
    assert "".join(d.text for d in deltas) == "The answer."
    assert deltas[-1].usage.prompt_tokens == 7 and deltas[-1].usage.completion_tokens == 3


async def test_missing_model_error_says_how_to_pull():
    http, _ = client(
        lambda p, b: httpx.Response(404, json={"error": "model 'qwen3.5:9b' not found"})
    )
    with pytest.raises(OllamaError, match=r"ollama pull qwen3\.5:9b"):
        await OllamaLLM(llm_profile(), client=http).generate(MESSAGES)
    with pytest.raises(OllamaError, match="ollama pull"):
        [d async for d in OllamaLLM(llm_profile(), client=http).stream(MESSAGES)]


async def test_unreachable_server_is_a_clear_error():
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    http = httpx.AsyncClient(base_url="http://ollama.test", transport=httpx.MockTransport(refuse))
    with pytest.raises(OllamaError, match="OLLAMA_BASE_URL"):
        await OllamaLLM(llm_profile(), client=http).generate(MESSAGES)


def test_unknown_params_and_options_are_rejected():
    with pytest.raises(ValueError, match="allowed"):
        OllamaLLM(llm_profile(temprature=0.1))
    llm = OllamaLLM(llm_profile())
    with pytest.raises(ValueError, match="top_q"):
        llm._payload(MESSAGES, False, {"top_q": 1})


def test_strip_thinking():
    assert strip_thinking("<think>a\nb</think>  Answer") == "Answer"
    assert strip_thinking("No thinking") == "No thinking"


def embed_profile(**params) -> EmbeddingProfile:
    return EmbeddingProfile(provider="ollama", model="nomic-embed-text", dims=3, params=params)


async def test_embedder_adds_task_prefixes_and_checks_dims():
    def handler(path, body):
        assert path == "/api/embed"
        return httpx.Response(
            200,
            json={"embeddings": [[0.1, 0.2, 0.3] for _ in body["input"]], "prompt_eval_count": 9},
        )

    http, rec = client(handler)
    emb = OllamaEmbedder(
        embed_profile(document_prefix="search_document: ", query_prefix="search_query: "),
        client=http,
    )
    vectors, usage = await emb.embed_documents(["a", "b"])
    assert len(vectors) == 2 and usage.embedding_tokens == 9
    assert rec.requests[0]["body"]["input"] == ["search_document: a", "search_document: b"]
    assert rec.requests[0]["body"]["truncate"] is True
    vector, _ = await emb.embed_query("q")
    assert vector == [0.1, 0.2, 0.3]
    assert rec.requests[1]["body"]["input"] == ["search_query: q"]
    assert await emb.embed_documents([]) == ([], emb_usage_zero())


def emb_usage_zero():
    from chatbot.core.types import Usage

    return Usage()


async def test_embedder_refuses_wrong_dimensions():
    http, _ = client(lambda p, b: httpx.Response(200, json={"embeddings": [[0.1, 0.2]]}))
    with pytest.raises(OllamaError, match="returned 2 dims"):
        await OllamaEmbedder(embed_profile(), client=http).embed_query("q")


def test_bearer_token_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_KEY", "secret")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/")
    llm = OllamaLLM(LLMProfile(provider="ollama", model="m", api_key_env="OLLAMA_KEY"))
    assert llm.client.headers["Authorization"] == "Bearer secret"
    assert str(llm.client.base_url) == "http://gpu-box:11434"


def test_local_ollama_profile_builds_ollama_components():
    from chatbot.core.settings import load_settings

    settings = load_settings(root=ROOT, profile="local-ollama", environ={})
    assert settings.vector_store.index == "docs_local"
    container.load_modules()
    assert isinstance(container.make_llm(settings, "answer"), OllamaLLM)
    emb = container.make_embedder(settings)
    assert isinstance(emb, OllamaEmbedder) and emb.dims == 768
    assert emb.query_prefix == "search_query: "
