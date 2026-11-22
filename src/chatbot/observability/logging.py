"""Structured logging with a trace ID per request. Message text is excluded by default."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_trace_id(trace_id: str | None) -> contextvars.Token[str | None]:
    return _trace_id.set(trace_id)


def reset_trace_id(token: contextvars.Token[str | None]) -> None:
    _trace_id.reset(token)


def get_trace_id() -> str:
    tid = _trace_id.get()
    if tid is None:
        tid = new_trace_id()
        _trace_id.set(tid)
    return tid


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": _trace_id.get(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        tid = _trace_id.get()
        base = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        if tid:
            base += f" trace_id={tid[:12]}"
        if extras:
            base += f" {extras}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "openai", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
