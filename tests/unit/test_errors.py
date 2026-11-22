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
