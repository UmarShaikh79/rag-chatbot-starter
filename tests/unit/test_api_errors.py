"""Failed requests answer JSON with a trace ID, never Starlette's plain-text 500.

A provider failure (model, embeddings, reranker) is 502: the request was fine, the upstream
was not. Anything else is 500 "internal error". Streamed and non-streamed requests report
the same message, and SDK messages (which can repeat part of an API key) are not shown.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from chatbot.api.app import create_app
from chatbot.core import container
from chatbot.core.errors import ChatbotError
from chatbot.providers.embeddings.hashing import HashingEmbedder
from chatbot.providers.llm.fake import FakeLLM
from chatbot.providers.ollama_client import OllamaError
from chatbot.testing.fakes import InMemoryJobQueue, InMemoryMemoryStore, InMemoryVectorStore
from tests.conftest import offline_settings
from tests.unit.test_api import ingest_samples, parse_sse

QUESTION = {"message": "Where are notes stored by default?"}


class FailingLLM(FakeLLM):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    async def generate(self, messages, *, schema=None, **opts):
        raise self.exc

    async def stream(self, messages, **opts):
        raise self.exc
        yield  # an async generator


def client_with(exc: Exception) -> TestClient:
    settings = offline_settings()
    c = container.build(
        settings,
        pool=None,
        answer_llm=FailingLLM(exc),
        cheap_llm=FakeLLM(),
        embedder=HashingEmbedder(384),
        store=InMemoryVectorStore(),
        memory=InMemoryMemoryStore(),
        queue=InMemoryJobQueue(),
    )
    client = TestClient(create_app(settings, components=c), raise_server_exceptions=False)
    client.components = c
    return client


def failed(client: TestClient) -> tuple[httpx.Response, list[tuple[str, dict]]]:
    ingest_samples(client)
    resp = client.post("/v1/chat", json=QUESTION)
    stream = client.post("/v1/chat", json={**QUESTION, "stream": True})
    assert stream.status_code == 200
    return resp, parse_sse(stream.text)


def assert_json_error(resp: httpx.Response, status: int, detail: str) -> None:
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["detail"] == detail
    assert body["trace_id"] and body["trace_id"] == resp.headers["x-trace-id"]


def test_provider_connection_failure_is_502():
    with client_with(httpx.ConnectError("connection refused")) as client:
        resp, events = failed(client)
    detail = "the model provider failed (ConnectError)"
    assert_json_error(resp, 502, detail)
    assert events[-1] == ("error", {"message": detail, "trace_id": events[-1][1]["trace_id"]})


def test_provider_error_of_ours_keeps_its_message():
    exc = OllamaError("Ollama model qwen3.5:9b is not pulled: run `make ollama-pull`")
    with client_with(exc) as client:
        resp, events = failed(client)
    assert_json_error(resp, 502, str(exc))
    assert events[-1][1]["message"] == str(exc)


def test_sdk_messages_are_not_shown():
    openai = pytest.importorskip("openai")
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    exc = openai.AuthenticationError(
        "Incorrect API key provided: sk-proj-abc*****************wxyz",
        response=httpx.Response(401, request=request),
        body=None,
    )
    with client_with(exc) as client:
        resp, events = failed(client)
    assert_json_error(resp, 502, "the model provider failed (AuthenticationError)")
    assert "wxyz" not in resp.text and "wxyz" not in str(events)


def test_template_errors_are_500_with_their_message():
    with client_with(ChatbotError("the answer prompt is missing")) as client:
        resp, events = failed(client)
    assert_json_error(resp, 500, "the answer prompt is missing")
    assert events[-1][1]["message"] == "the answer prompt is missing"


def test_unexpected_errors_are_json_500_without_internals():
    with client_with(RuntimeError("password=hunter2 in a stack frame")) as client:
        resp, events = failed(client)
    assert_json_error(resp, 500, "internal error")
    assert "hunter2" not in resp.text
    assert events[-1][1]["message"] == "internal error"
