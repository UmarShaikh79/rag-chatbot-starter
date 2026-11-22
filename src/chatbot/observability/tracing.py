"""Tracing and metrics behind one small facade (ADR 026).

Code everywhere calls ``span(...)``, ``count(...)`` and ``observe(...)`` from this module.
Until ``configure_telemetry`` turns a backend on they do nothing, and OpenTelemetry is not
even imported, so the base install stays light::

    with span("retrieve", type="retriever", attrs={"chatbot.retrieval.top_k": 8}) as s:
        hits = ...
        s.set({"chatbot.retrieval.hits": len(hits)})
        s.io(input=query, output=[h.chunk.id for h in hits])

Backends (``observability.otel``, ``observability.langfuse``) share one OpenTelemetry
tracer provider; each gets its own exporter. Spans use the OpenTelemetry GenAI conventions
(``gen_ai.*``) plus the ``langfuse.*`` attributes Langfuse reads, so the same span is a
generation in Langfuse and a normal span in Jaeger, Tempo or Honeycomb.

Text (prompts, answers, retrieved chunks) is recorded only through ``SpanHandle.io`` and
only when a backend has ``capture_text`` on; a backend with it off gets the span without
the text attributes.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from chatbot import __version__

if TYPE_CHECKING:
    from chatbot.core.settings import Settings

log = logging.getLogger(__name__)

SCOPE = "chatbot"  # instrumentation scope of every span this template creates
HTTP_SCOPE = "chatbot.http"  # server spans from TraceMiddleware (not sent to Langfuse)
ROOT_ATTR = "chatbot.root"  # marks the span a Langfuse trace starts at (a chat turn, a job)

# Attributes that carry text. Stripped for backends with capture_text off.
INPUT_ATTR = "langfuse.observation.input"
OUTPUT_ATTR = "langfuse.observation.output"
TEXT_ATTRS = (INPUT_ATTR, OUTPUT_ATTR)
USER_ATTR = "langfuse.user.id"
SESSION_ATTR = "langfuse.session.id"
MAX_TEXT_CHARS = 20_000  # per attribute; longer text is cut

AttrValue = str | bool | int | float | list[str] | list[int] | list[float]

# Trace-level attributes (user, session) copied onto every span of the current turn, which
# is what Langfuse needs to filter by them (they must be on every span, not only the root).
_trace_attrs: contextvars.ContextVar[dict[str, AttrValue] | None] = contextvars.ContextVar(
    "trace_attrs", default=None
)


_root: contextvars.ContextVar[Any] = contextvars.ContextVar("root_span", default=None)


def root_io(input: Any = None, output: Any = None) -> None:
    """Input and output of the current turn or job (the trace's root observation)."""
    handle = _root.get()
    if handle is not None:
        handle.io(input=input, output=output)


def _clean(attrs: Mapping[str, Any] | None) -> dict[str, AttrValue]:
    out: dict[str, AttrValue] = {}
    for key, value in (attrs or {}).items():
        if value is None:
            continue
        if isinstance(value, str | bool | int | float):
            out[key] = value
        elif isinstance(value, list | tuple) and all(isinstance(v, str) for v in value):
            out[key] = list(value)
        else:
            out[key] = json.dumps(value, default=str, ensure_ascii=False)
    return out


def _text(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + "…"


class SpanHandle:
    """What a ``span`` block gets. The no-op version ignores everything."""

    trace_id: str | None = None

    def set(self, attrs: Mapping[str, Any]) -> None:
        pass

    def io(self, input: Any = None, output: Any = None) -> None:
        pass

    def usage(self, prompt: int, completion: int, cost: float | None) -> None:
        pass

    def fail(self, exc: BaseException | str) -> None:
        pass

    def rename(self, name: str) -> None:
        pass


_NOOP = SpanHandle()


class Telemetry:
    """No-op telemetry: the default until a backend is configured."""

    enabled = False
    capture_text = False

    @contextmanager
    def span(
        self,
        name: str,
        *,
        type: str = "span",
        attrs: Mapping[str, Any] | None = None,
        root: bool = False,
        server: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> Iterator[SpanHandle]:
        yield _NOOP

    def count(self, name: str, value: float = 1, attrs: Mapping[str, Any] | None = None) -> None:
        pass

    def observe(self, name: str, value: float, attrs: Mapping[str, Any] | None = None) -> None:
        pass

    def shutdown(self) -> None:
        pass


_telemetry: Telemetry = Telemetry()


def telemetry() -> Telemetry:
    return _telemetry


def span(name: str, **kwargs: Any):
    return _telemetry.span(name, **kwargs)


def count(name: str, value: float = 1, attrs: Mapping[str, Any] | None = None) -> None:
    _telemetry.count(name, value, attrs)


def observe(name: str, value: float, attrs: Mapping[str, Any] | None = None) -> None:
    _telemetry.observe(name, value, attrs)


@contextmanager
def trace_attributes(user_id: str | None = None, session_id: str | None = None) -> Iterator[None]:
    """User and session (conversation) for every span opened inside the block."""
    attrs = dict(_trace_attrs.get() or {})
    if user_id:
        attrs[USER_ATTR] = user_id
    if session_id:
        attrs[SESSION_ATTR] = session_id
    token = _trace_attrs.set(attrs)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):  # stream closed from another context
            _trace_attrs.reset(token)


@contextmanager
def timed() -> Iterator[list[float]]:
    """``with timed() as t: ...`` then ``t[0]`` is the elapsed seconds."""
    out = [0.0]
    started = time.perf_counter()
    try:
        yield out
    finally:
        out[0] = time.perf_counter() - started


# ---------------------------------------------------------------- OpenTelemetry


class _OtelSpan(SpanHandle):
    def __init__(self, span: Any, capture: bool) -> None:
        self._span = span
        self._capture = capture
        ctx = span.get_span_context()
        self.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None

    def set(self, attrs: Mapping[str, Any]) -> None:
        self._span.set_attributes(_clean(attrs))

    def io(self, input: Any = None, output: Any = None) -> None:
        if not self._capture:
            return
        if input is not None:
            self._span.set_attribute(INPUT_ATTR, _text(input))
        if output is not None:
            self._span.set_attribute(OUTPUT_ATTR, _text(output))

    def usage(self, prompt: int, completion: int, cost: float | None) -> None:
        attrs: dict[str, Any] = {
            "gen_ai.usage.input_tokens": prompt,
            "gen_ai.usage.output_tokens": completion,
            "langfuse.observation.usage_details": {"input": prompt, "output": completion},
        }
        if cost is not None:
            attrs["gen_ai.usage.cost"] = cost
            attrs["langfuse.observation.cost_details"] = {"total": cost}
        self.set(attrs)

    def fail(self, exc: BaseException | str) -> None:
        from opentelemetry.trace import Status, StatusCode

        message = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        if isinstance(exc, BaseException):
            self._span.record_exception(exc)
        self._span.set_status(Status(StatusCode.ERROR, message))
        self._span.set_attribute("langfuse.observation.level", "ERROR")

    def rename(self, name: str) -> None:
        self._span.update_name(name)


class OtelTelemetry(Telemetry):
    enabled = True

    def __init__(
        self,
        tracer_provider: Any,
        meter_provider: Any | None,
        capture_text: bool,
        static_attrs: Mapping[str, Any] | None = None,
    ) -> None:
        from opentelemetry import propagate, trace

        self._trace = trace
        self._propagate = propagate
        self.tracer_provider = tracer_provider
        self.meter_provider = meter_provider
        self.tracer = tracer_provider.get_tracer(SCOPE, __version__)
        self.http_tracer = tracer_provider.get_tracer(HTTP_SCOPE, __version__)
        self.meter = meter_provider.get_meter(SCOPE, __version__) if meter_provider else None
        self.capture_text = capture_text
        self.static_attrs = _clean(static_attrs)
        self._instruments: dict[str, Any] = {}

    @contextmanager
    def span(
        self,
        name: str,
        *,
        type: str = "span",
        attrs: Mapping[str, Any] | None = None,
        root: bool = False,
        server: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> Iterator[SpanHandle]:
        trace = self._trace
        context = self._propagate.extract(dict(headers)) if headers is not None else None
        tracer = self.http_tracer if server else self.tracer
        kind = trace.SpanKind.SERVER if server else trace.SpanKind.INTERNAL
        with tracer.start_as_current_span(
            name,
            context=context,
            kind=kind,
            record_exception=False,
            set_status_on_exception=False,
        ) as raw:
            handle = _OtelSpan(raw, self.capture_text)
            base: dict[str, Any] = {**self.static_attrs, **(_trace_attrs.get() or {})}
            if not server:
                base["langfuse.observation.type"] = type
            if root:
                base[ROOT_ATTR] = True
                base["langfuse.trace.name"] = name
            handle.set({**base, **(attrs or {})})
            token = _root.set(handle) if root else None
            try:
                yield handle
            except BaseException as exc:
                if not isinstance(exc, GeneratorExit):
                    handle.fail(exc)
                raise
            finally:
                if token is not None:
                    with contextlib.suppress(ValueError):  # stream closed from another context
                        _root.reset(token)

    def _instrument(self, kind: str, name: str) -> Any:
        key = f"{kind}:{name}"
        inst = self._instruments.get(key)
        if inst is None and self.meter is not None:
            unit = METRIC_UNITS.get(name, "1")
            maker = self.meter.create_counter if kind == "counter" else self.meter.create_histogram
            inst = self._instruments[key] = maker(name, unit=unit)
        return inst

    def count(self, name: str, value: float = 1, attrs: Mapping[str, Any] | None = None) -> None:
        inst = self._instrument("counter", name)
        if inst is not None:
            inst.add(value, _clean(attrs))

    def observe(self, name: str, value: float, attrs: Mapping[str, Any] | None = None) -> None:
        inst = self._instrument("histogram", name)
        if inst is not None:
            inst.record(value, _clean(attrs))

    def shutdown(self) -> None:
        for provider in (self.tracer_provider, self.meter_provider):
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:  # never fail a shutdown over telemetry
                    log.warning("telemetry shutdown failed", exc_info=True)


# Units of the metrics the template emits (docs/observability.md lists them all).
METRIC_UNITS = {
    "chatbot.chat.turns": "{turn}",
    "chatbot.chat.duration": "s",
    "chatbot.retrieval.requests": "{request}",
    "chatbot.guard.blocks": "{block}",
    "chatbot.cost": "USD",
    "chatbot.jobs": "{job}",
    "chatbot.jobs.duration": "s",
    "gen_ai.client.token.usage": "{token}",
    "gen_ai.client.operation.duration": "s",
    "http.server.request.duration": "s",
}


def _filtering_exporter(
    inner: Any, *, only_scope: str | None, strip: tuple[str, ...], detach_roots: bool
) -> Any:
    """Wrap a span exporter: keep one instrumentation scope, drop text attributes, and make
    ``chatbot.root`` spans roots (their HTTP parent is not exported to that backend)."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class FilteringExporter(SpanExporter):
        def export(self, spans):  # type: ignore[no-untyped-def]
            out = []
            for s in spans:
                scope = s.instrumentation_scope.name if s.instrumentation_scope else None
                if only_scope is not None and scope != only_scope:
                    continue
                attrs = dict(s.attributes or {})
                is_root = detach_roots and attrs.get(ROOT_ATTR) is True and s.parent is not None
                if is_root or any(k in attrs for k in strip):
                    s = ReadableSpan(
                        name=s.name,
                        context=s.context,
                        parent=None if is_root else s.parent,
                        resource=s.resource,
                        attributes={k: v for k, v in attrs.items() if k not in strip},
                        events=s.events,
                        links=s.links,
                        kind=s.kind,
                        status=s.status,
                        start_time=s.start_time,
                        end_time=s.end_time,
                        instrumentation_scope=s.instrumentation_scope,
                    )
                out.append(s)
            return inner.export(out) if out else SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            inner.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return inner.force_flush(timeout_millis)

    return FilteringExporter()


def langfuse_endpoint(settings: Settings, environ: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """Langfuse's OTLP traces URL and headers (Basic auth from the public and secret key)."""
    import base64

    cfg = settings.observability.langfuse
    base = (environ.get(cfg.base_url_env) or cfg.base_url).rstrip("/")
    public, secret = environ.get(cfg.public_key_env), environ.get(cfg.secret_key_env)
    if not public or not secret:
        raise RuntimeError(
            f"observability.langfuse is enabled: set {cfg.public_key_env} and "
            f"{cfg.secret_key_env} (Langfuse project settings -> API keys)"
        )
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
        "x-langfuse-ingestion-version": "4",  # real-time ingestion of OTLP data
    }
    return f"{base}/api/public/otel/v1/traces", headers


def build_otel(
    settings: Settings,
    service: str,
    environ: Mapping[str, str] | None = None,
    span_exporters: list[Any] | None = None,
    metric_reader: Any | None = None,
) -> OtelTelemetry:
    """The OpenTelemetry providers for the configured backends. Tests pass in-memory
    exporters and a metric reader instead of the OTLP ones."""
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:
        raise RuntimeError(
            "observability.otel / observability.langfuse need the OpenTelemetry SDK: "
            "uv sync --extra otel (or --extra langfuse)"
        ) from exc

    environ = os.environ if environ is None else environ
    obs = settings.observability
    otel, lf = obs.otel, obs.langfuse
    resource = Resource.create(
        {
            "service.name": environ.get("OTEL_SERVICE_NAME")
            or otel.service_name
            or settings.app.name,
            "service.version": __version__,
            "service.namespace": "chatbot",
            "chatbot.role": service,  # api | worker | cli
            "deployment.environment.name": settings.app.env,
        }
    )
    tracer_provider = TracerProvider(
        resource=resource, sampler=ParentBased(TraceIdRatioBased(otel.sample_ratio))
    )
    if span_exporters is not None:
        for exporter in span_exporters:
            tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        if otel.enabled and otel.traces:
            exporter = OTLPSpanExporter()  # endpoint and headers from OTEL_EXPORTER_OTLP_*
            strip = () if otel.capture_text else TEXT_ATTRS
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    _filtering_exporter(exporter, only_scope=None, strip=strip, detach_roots=False)
                )
            )
        if otel.enabled and otel.console:
            tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        if lf.enabled:
            endpoint, headers = langfuse_endpoint(settings, environ)
            strip = () if lf.capture_text else TEXT_ATTRS
            if not lf.send_user_id:
                strip = (*strip, USER_ATTR)
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    _filtering_exporter(
                        OTLPSpanExporter(endpoint=endpoint, headers=headers),
                        only_scope=SCOPE,  # LLM-relevant spans only, no HTTP or DB noise
                        strip=strip,
                        detach_roots=True,
                    )
                )
            )

    meter_provider = None
    if otel.enabled and otel.metrics:
        if metric_reader is None:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(), export_interval_millis=int(otel.metrics_interval_s * 1000)
            )
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])

    # Text goes on spans when any backend wants it; the others get it stripped.
    capture = (otel.enabled and otel.capture_text) or (lf.enabled and lf.capture_text)
    return OtelTelemetry(
        tracer_provider,
        meter_provider,
        capture_text=capture,
        static_attrs={"langfuse.environment": settings.app.env, "langfuse.release": __version__},
    )


def configure_telemetry(settings: Settings, service: str = "api", **kwargs: Any) -> Telemetry:
    """Install the configured backends (or the no-op) as the process-wide telemetry.

    Idempotent per process: a second call shuts the previous providers down first. The
    tracer provider also becomes OpenTelemetry's global one, so any library instrumentation
    you add (httpx, psycopg, ...) lands in the same traces.
    """
    global _telemetry
    obs = settings.observability
    previous = _telemetry
    if not (obs.otel.enabled or obs.langfuse.enabled):
        _telemetry = Telemetry()
    else:
        _telemetry = build_otel(settings, service, **kwargs)
        if not kwargs:
            from opentelemetry import metrics, trace

            trace.set_tracer_provider(_telemetry.tracer_provider)  # type: ignore[attr-defined]
            if _telemetry.meter_provider is not None:  # type: ignore[attr-defined]
                metrics.set_meter_provider(_telemetry.meter_provider)  # type: ignore[attr-defined]
        log.info(
            "telemetry on",
            extra={"otel": obs.otel.enabled, "langfuse": obs.langfuse.enabled, "role": service},
        )
    if previous is not _telemetry:
        previous.shutdown()
    return _telemetry


def shutdown_telemetry() -> None:
    """Flush and stop the exporters (end of the API lifespan, worker exit, CLI command)."""
    global _telemetry
    _telemetry.shutdown()
    _telemetry = Telemetry()
