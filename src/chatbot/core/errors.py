import re


class ChatbotError(Exception):
    """Base error for the template.

    ``permanent``: the same work would fail the same way if retried (a background job is then
    marked failed at once instead of re-queued). None: decide from the HTTP status, if any.
    """

    permanent: bool | None = None


class IndexMismatchError(ChatbotError):
    """The configured embedding model differs from the one the index was built with (ADR 005)."""

    permanent = True


class NotFoundError(ChatbotError):
    permanent = True


class ForbiddenError(ChatbotError):
    permanent = True


class ProviderError(ChatbotError):
    """A model, embedding or rerank provider failed after its own retries (down, rate-limited,
    bad key, model missing). The API answers 502: the request was fine, the upstream was not.
    ``status`` is the upstream's HTTP status, when there was one."""

    def __init__(
        self, message: str, status: int | None = None, permanent: bool | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status
        if permanent is not None:
            self.permanent = permanent


# 4xx statuses that can succeed on a later attempt (timeout, conflict, too early, rate limit).
RETRYABLE_4XX = frozenset({408, 409, 425, 429})


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)  # OpenAI SDK errors, ProviderError
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)  # httpx.HTTPStatusError
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_permanent(exc: BaseException) -> bool:
    """True when retrying cannot help: bad credentials or permissions (401, 403), not found,
    bad request, or one of our errors marked ``permanent``. Follows ``raise ... from`` causes,
    so a source that wraps an HTTP error keeps its meaning. Unknown errors are not permanent."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        flag = getattr(current, "permanent", None)
        if isinstance(flag, bool):
            return flag
        status = _status_of(current)
        if status is not None:
            return 400 <= status < 500 and status not in RETRYABLE_4XX
        context = None if current.__suppress_context__ else current.__context__
        current = current.__cause__ or context
    return False


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
        return 502, scrub_secrets(str(exc))
    if _sdk_provider_error(exc):
        return 502, f"the model provider failed ({type(exc).__name__})"
    if isinstance(exc, ChatbotError):
        return 500, scrub_secrets(str(exc))
    return 500, "internal error"


# Credentials that can appear in error messages: provider keys (OpenAI echoes a masked key in
# its 401 message), this template's API keys, JWTs, bearer tokens, URL passwords, key=value.
_SECRETS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_*-]{8,}"), "[redacted]"),
    (re.compile(r"\bcb_[A-Za-z0-9_-]{20,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[redacted]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.I), r"\1[redacted]@"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|token)"
            r"([\"']?\s*[=:]\s*)[\"']?[^\s\"',;&]{6,}"
        ),
        r"\1\2[redacted]",
    ),
]


def scrub_secrets(text: str) -> str:
    """``text`` with anything that looks like a credential replaced by ``[redacted]``."""
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def error_text(exc: BaseException) -> str:
    """``"Type: message"`` for storing or showing an error, with credentials scrubbed. Use it
    wherever an error leaves the process: job ``last_error`` and results, span status
    (exported to your tracing backend), reports."""
    return scrub_secrets(f"{type(exc).__name__}: {exc}")
