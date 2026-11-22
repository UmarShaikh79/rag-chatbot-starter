class ChatbotError(Exception):
    """Base error for the template."""


class IndexMismatchError(ChatbotError):
    """The configured embedding model differs from the one the index was built with (ADR 005)."""


class NotFoundError(ChatbotError):
    pass


class ForbiddenError(ChatbotError):
    pass


class ProviderError(ChatbotError):
    """A model, embedding or rerank provider failed after its own retries (down, rate-limited,
    bad key, model missing). The API answers 502: the request was fine, the upstream was not."""


def _sdk_provider_error(exc: BaseException) -> bool:
    """Errors from the HTTP clients and SDKs providers use, which don't subclass ours."""
    import httpx

    if isinstance(exc, httpx.HTTPError):
        return True
    try:
        from openai import OpenAIError
    except ImportError:
        return False
    return isinstance(exc, OpenAIError)


def describe_error(exc: BaseException) -> tuple[int, str]:
    """HTTP status and a message safe to show a client, for a request that failed.

    SDK messages are not shown: an OpenAI 401 message, for one, repeats part of the API key.
    The full error is in the log line with the same trace ID.
    """
    if isinstance(exc, ProviderError):
        return 502, str(exc)
    if _sdk_provider_error(exc):
        return 502, f"the model provider failed ({type(exc).__name__})"
    if isinstance(exc, ChatbotError):
        return 500, str(exc)
    return 500, "internal error"
