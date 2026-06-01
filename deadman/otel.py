"""OpenTelemetry instrumentation for DEADMAN.

Usage
-----
    from deadman.otel import init_otel, audit_span

    # In webhook.py:
    init_otel(app)          # FastAPI app (or None for non-HTTP use)

    # Wrapping an MCP audit write:
    with audit_span("mcp.execute", {"tool": "github.revert_pr", "key": key}):
        audit.write(...)

Design notes
------------
- All OTel packages are lazily imported inside the functions so that the base
  image (mock mode) works with zero OTel packages installed — if they're absent
  every function silently no-ops.
- When OTEL_EXPORTER_OTLP_ENDPOINT is unset the module is also a no-op, so
  mock mode has zero overhead and zero extra dependencies.
- The MCP Gateway audit log IS the OTel export path described in
  infra/guardrails.yaml: each COMMITTED / DENIED / SKIPPED_IDEMPOTENT audit
  record is wrapped in an audit_span so the OTLP collector receives a span per
  governed tool call. The guardrails.yaml `audit_log: otel` stanza refers to
  this span pipeline.
"""
from __future__ import annotations
import contextlib

import deadman.config as config

# Module-level handle — set to a real TracerProvider if OTel is configured.
_tracer = None
_otel_active = False


def init_otel(app=None) -> None:
    """Initialise OpenTelemetry tracing.

    If OTEL_EXPORTER_OTLP_ENDPOINT is not set this is a no-op. If the otel
    packages are not installed this is also a no-op (logs a warning instead of
    crashing).

    Parameters
    ----------
    app:
        FastAPI application instance. When provided and the
        opentelemetry-instrumentation-fastapi package is installed, FastAPI
        will be auto-instrumented so every HTTP request becomes a trace span.
    """
    global _tracer, _otel_active

    endpoint = config.OTEL_EXPORTER_OTLP_ENDPOINT
    if not endpoint:
        return  # no-op in mock mode / when OTel is not configured

    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore
    except ImportError:
        import warnings
        warnings.warn(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry packages are "
            "not installed. Tracing is disabled. Install the [otel] extras to "
            "enable it.",
            stacklevel=2,
        )
        return

    resource = Resource.create({
        "service.name": config.OTEL_SERVICE_NAME,
        "deployment.environment": config.MODE,
    })
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(config.OTEL_SERVICE_NAME)
    _otel_active = True

    # Instrument FastAPI if available.
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # type: ignore
            FastAPIInstrumentor.instrument_app(app)
        except ImportError:
            pass  # instrumentation package not installed; HTTP spans unavailable


@contextlib.contextmanager
def audit_span(name: str, attributes: dict | None = None):
    """Context manager that wraps a block in an OTel span named *name*.

    When OTel is not active (mock mode or packages absent) this is a no-op
    context manager — zero overhead, zero import-time requirements.

    Parameters
    ----------
    name:
        Span name, e.g. ``"mcp.execute"`` or ``"audit.committed"``.
    attributes:
        Key-value pairs attached to the span, e.g.
        ``{"tool": "github.revert_pr", "key": "incident-42::revert_pr::PR-1337"}``.

    Example
    -------
    ::

        with audit_span("mcp.execute", {"tool": tool, "key": key, "status": "COMMITTED"}):
            audit.write({"status": "COMMITTED", "tool": tool, "key": key})
    """
    if not _otel_active or _tracer is None:
        yield
        return

    with _tracer.start_as_current_span(name) as span:
        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, str(v))
        yield span
