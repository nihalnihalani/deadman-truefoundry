"""DEADMAN Prometheus metrics.

Design: prometheus_client is OPTIONAL.  All metric objects are either real
prometheus_client instances or lightweight no-op stubs so that mock mode and CI
(without prometheus_client installed) never crash.

LEAD NOTE: Add ``prometheus-client>=0.20`` to requirements.txt to enable real
metric collection.  Everything degrades gracefully to no-ops without it.

Public API
----------
record_incident(mode, outcome)          Counter: deadman_incidents_total
record_fallback_depth(depth)            Histogram: deadman_fallback_depth
record_double_execution()               Counter: deadman_double_executions_total
record_guardrail_block(tool)            Counter: deadman_guardrail_blocks_total
record_scope_denied(tool)               Counter: deadman_scope_denied_total
observe_tool(tool, result, latency_s)   Counter+Histogram: deadman_tool_calls_total / _latency_seconds
render() -> (bytes, str)                Prometheus text-exposition bytes + content_type
"""
from __future__ import annotations

import time
import contextlib
from typing import Generator


# ── Try to import prometheus_client; fall back to no-op stubs ─────────────────

try:
    import prometheus_client as _prom
    from prometheus_client import Counter, Histogram, CONTENT_TYPE_LATEST, generate_latest
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover — tested via monkeypatch in test suite
    _prom = None
    _PROM_AVAILABLE = False


# ── No-op stub classes (used when prometheus_client is absent) ─────────────────

class _NoOpCounter:
    """No-op stub for prometheus_client.Counter."""
    def labels(self, **_kw):
        return self
    def inc(self, amount=1):
        pass


class _NoOpHistogram:
    """No-op stub for prometheus_client.Histogram."""
    def labels(self, **_kw):
        return self
    def observe(self, value):
        pass


# ── Metric definitions ─────────────────────────────────────────────────────────

def _counter(name: str, doc: str, labelnames: list):
    if _PROM_AVAILABLE:
        return Counter(name, doc, labelnames)
    return _NoOpCounter()


def _histogram(name: str, doc: str, labelnames: list, buckets=None):
    if _PROM_AVAILABLE:
        kwargs = {"buckets": buckets} if buckets else {}
        return Histogram(name, doc, labelnames, **kwargs)
    return _NoOpHistogram()


incidents_total = _counter(
    "deadman_incidents_total",
    "Total incidents processed",
    ["mode", "outcome"],
)

fallback_depth = _histogram(
    "deadman_fallback_depth",
    "AI Gateway fallback depth at the end of an incident run",
    [],
    buckets=[0, 1, 2, 3, 4, 5],
)

double_executions_total = _counter(
    "deadman_double_executions_total",
    "Number of double-execution events detected",
    [],
)

guardrail_blocks_total = _counter(
    "deadman_guardrail_blocks_total",
    "Pre/Post tool guardrail blocks by tool",
    ["tool"],
)

tool_calls_total = _counter(
    "deadman_tool_calls_total",
    "MCP tool calls by tool name and result",
    ["tool", "result"],
)

tool_latency_seconds = _histogram(
    "deadman_tool_latency_seconds",
    "MCP tool call latency in seconds",
    ["tool"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
)

scope_denied_total = _counter(
    "deadman_scope_denied_total",
    "Cedar scope-denied events by tool",
    ["tool"],
)


# ── Helper functions (all safe no-ops when prometheus_client absent) ───────────

def record_incident(mode: str, outcome: str) -> None:
    """Increment deadman_incidents_total{mode, outcome}."""
    incidents_total.labels(mode=mode, outcome=outcome).inc()


def record_fallback_depth(depth: int) -> None:
    """Record the final fallback depth for an incident."""
    fallback_depth.observe(depth)


def record_double_execution() -> None:
    """Increment deadman_double_executions_total."""
    double_executions_total.inc()


def record_guardrail_block(tool: str) -> None:
    """Increment deadman_guardrail_blocks_total{tool}."""
    guardrail_blocks_total.labels(tool=tool).inc()


def record_scope_denied(tool: str) -> None:
    """Increment deadman_scope_denied_total{tool}."""
    scope_denied_total.labels(tool=tool).inc()


def observe_tool(tool: str, result: str, latency_s: float) -> None:
    """Record a tool call result and latency.

    Parameters
    ----------
    tool:      Tool name (e.g. "github.revert_pr")
    result:    "EXECUTED" | "SKIPPED_IDEMPOTENT" | "ERROR"
    latency_s: Wall-clock seconds for the tool call
    """
    tool_calls_total.labels(tool=tool, result=result).inc()
    tool_latency_seconds.labels(tool=tool).observe(latency_s)


def render() -> tuple[bytes, str]:
    """Return (Prometheus-exposition bytes, content_type string).

    When prometheus_client is absent, returns a minimal plain-text response so
    the /metrics endpoint always returns 200 rather than 500.
    """
    if _PROM_AVAILABLE:
        content = generate_latest()
        return content, CONTENT_TYPE_LATEST
    # Graceful degradation — no metrics to report
    body = b"# prometheus_client not installed; no metrics available\n"
    return body, "text/plain; version=0.0.4; charset=utf-8"
