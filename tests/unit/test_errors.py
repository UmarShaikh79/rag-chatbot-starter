"""Which failures are permanent (a retry would fail the same way) and which are worth retrying.

Background jobs use this: a permanent error fails the job at once instead of re-queuing it
up to ``max_attempts`` times.
"""

import httpx
import pytest

from chatbot.core.errors import (
    ChatbotError,
    ForbiddenError,
    IndexMismatchError,
    NotFoundError,
    ProviderError,
    is_permanent,
)
from chatbot.providers.ollama_client import OllamaError
from chatbot.providers.rerankers import RerankError

REQUEST = httpx.Request("POST", "https://api.example.com/v1/x")


def http_error(status: int) -> httpx.HTTPStatusError:
    response = httpx.Response(status, request=REQUEST)
    return httpx.HTTPStatusError(str(status), request=REQUEST, response=response)


@pytest.mark.parametrize(
    ("exc", "permanent"),
    [
        (http_error(400), True),
        (http_error(401), True),
        (http_error(403), True),
        (http_error(404), True),
        (http_error(422), True),
        (http_error(408), False),
        (http_error(409), False),
        (http_error(429), False),
        (http_error(500), False),
        (http_error(503), False),
        (httpx.ConnectError("refused"), False),
        (httpx.ReadTimeout("slow"), False),
        (OllamaError("model not found", 404), True),
        (OllamaError("Cannot reach Ollama"), False),
        (RerankError("rate limited", 429), False),
        (RerankError("Set COHERE_API_KEY", permanent=True), True),
        (ProviderError("upstream 401", 401), True),
        (NotFoundError("unknown source"), True),
        (ForbiddenError("not yours"), True),
        (IndexMismatchError("built with another model"), True),
        (ChatbotError("something else"), False),
        (RuntimeError("bug"), False),
    ],
)
def test_is_permanent(exc, permanent):
    assert is_permanent(exc) is permanent


def test_openai_sdk_errors():
    openai = pytest.importorskip("openai")

    def sdk(cls, status: int):
        return cls("x", response=httpx.Response(status, request=REQUEST), body=None)

    assert is_permanent(sdk(openai.PermissionDeniedError, 403))
    assert is_permanent(sdk(openai.AuthenticationError, 401))
    assert not is_permanent(sdk(openai.RateLimitError, 429))
    assert not is_permanent(openai.APIConnectionError(request=REQUEST))


def test_causes_are_followed_unless_suppressed():
    def wrapped(suppress: bool) -> BaseException:
        try:
            try:
                raise http_error(403)
            except httpx.HTTPStatusError as exc:
                if suppress:
                    raise ChatbotError("source failed") from None
                raise ChatbotError("source failed") from exc
        except ChatbotError as outer:
            return outer
        raise AssertionError

    assert is_permanent(wrapped(suppress=False))
    assert not is_permanent(wrapped(suppress=True))


# ---------------------------------------------------------------- credentials in error text

OPENAI_401 = (
    "Error code: 401 - {'error': {'message': 'Incorrect API key provided: "
    "sk-inval**********************wxyz. You can find your API key at "
    "https://platform.openai.com/account/api-keys.', 'type': 'invalid_request_error'}}"
)


@pytest.mark.parametrize(
    ("text", "secret", "kept"),
    [
        (OPENAI_401, "wxyz", "Incorrect API key provided: [redacted]. You can find"),
        ("key sk-proj-Abc123def456ghi789 rejected", "Abc123", "key [redacted] rejected"),
        ("langfuse pk-lf-0123456789abcdef and sk-lf-fedcba9876543210", "0123", "langfuse"),
        ("client cb_Zx8kQm2Lr5Tn9Vw3Yp6Hj4Gf1Ds7Ae0B failed", "Zx8kQm", "client [redacted]"),
        ("got eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2ln back", "eyJzdWIi", "got [redacted]"),
        ("header Authorization: Bearer abcdefghij123456", "abcdefghij", "Bearer [redacted]"),
        ("postgresql://reader:s3cretpw@db.example.com/app", "s3cretpw", "://[redacted]@db"),
        ("bad api_key=AbCdEf123456 in query", "AbCdEf", "api_key=[redacted]"),
        ('{"password": "hunter2hunter2"}', "hunter2", "password"),
    ],
)
def test_scrub_secrets_removes_credentials_and_keeps_the_rest(text, secret, kept):
    from chatbot.core.errors import scrub_secrets

    out = scrub_secrets(text)
    assert secret not in out and kept in out


@pytest.mark.parametrize(
    "text",
    [
        "token expired",
        "the HS256 secret must be at least 32 characters",
        "Cannot reach Ollama at http://127.0.0.1:11434: All connection attempts failed",
        "Set COHERE_API_KEY to use the Cohere reranker",
        "risk-assessment and task-specific settings",
        "Cohere rerank failed after 3 attempts: HTTP 429",
    ],
)
def test_scrub_secrets_leaves_ordinary_messages_alone(text):
    from chatbot.core.errors import scrub_secrets

    assert scrub_secrets(text) == text


def test_error_text_and_describe_error_scrub():
    from chatbot.core.errors import describe_error, error_text

    exc = ProviderError(f"upstream said: {OPENAI_401}", 401)
    assert error_text(exc).startswith("ProviderError: upstream said: Error code: 401")
    assert "wxyz" not in error_text(exc)
    assert describe_error(exc)[0] == 502 and "wxyz" not in describe_error(exc)[1]
