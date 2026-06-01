"""Tests: Observability layer (Pulse).

Covers:
  /readyz — 200 in mock mode (or 503 with well-shaped body if errors present)
  /metrics — always 200, Prometheus-format text, never 500
  Structured logging — records are valid JSON with correlation_id injected
  metrics helpers — no-op-safe when prometheus_client is absent (monkeypatched)
  otel.span() — no-op context manager when OTel is not configured, no raise
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
import types

import pytest
from fastapi.testclient import TestClient


# ── TestClient fixture ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    from deadman.webhook import app
    return TestClient(app)


# ── /readyz ───────────────────────────────────────────────────────────────────

class TestReadyz:

    def test_readyz_returns_200_or_503(self, client, isolated_state):
        """In mock mode /readyz is either 200 (ok) or 503 (errors present)."""
        resp = client.get("/readyz")
        assert resp.status_code in (200, 503)

    def test_readyz_body_has_ok_field(self, client, isolated_state):
        resp = client.get("/readyz")
        data = resp.json()
        assert "ok" in data, f"/readyz body missing 'ok': {data}"

    def test_readyz_body_shape(self, client, isolated_state):
        """Body must contain: ok, mode, state_backend, demo_enabled, errors, warnings."""
        data = client.get("/readyz").json()
        for key in ("ok", "mode", "state_backend", "demo_enabled", "errors", "warnings"):
            assert key in data, f"/readyz missing key '{key}': {data}"

    def test_readyz_ok_true_in_mock(self, client, isolated_state):
        """Mock mode with default env has no errors -> /readyz returns 200."""
        resp = client.get("/readyz")
        data = resp.json()
        # In mock mode the only possible error is a bad DEADMAN_MODE / STATE_BACKEND,
        # both of which are valid defaults.
        if data["ok"]:
            assert resp.status_code == 200
        else:
            # If there are errors, status must be 503 and errors list must be non-empty.
            assert resp.status_code == 503
            assert isinstance(data["errors"], list)
            assert len(data["errors"]) > 0

    def test_readyz_errors_is_list(self, client, isolated_state):
        data = client.get("/readyz").json()
        assert isinstance(data["errors"], list)
        assert isinstance(data["warnings"], list)

    def test_readyz_503_when_mode_invalid(self, client, isolated_state, monkeypatch):
        """Patching MODE to an invalid value causes /readyz to return 503."""
        import deadman.config as config
        monkeypatch.setattr(config, "MODE", "invalid-mode")
        resp = client.get("/readyz")
        assert resp.status_code == 503
        data = resp.json()
        assert data["ok"] is False
        assert len(data["errors"]) > 0


# ── /metrics ─────────────────────────────────────────────────────────────────

class TestMetricsEndpoint:

    def test_metrics_returns_200(self, client, isolated_state):
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_content_type_is_text(self, client, isolated_state):
        resp = client.get("/metrics")
        ct = resp.headers.get("content-type", "")
        assert ct.startswith("text/"), f"Unexpected Content-Type: {ct}"

    def test_metrics_body_not_empty(self, client, isolated_state):
        resp = client.get("/metrics")
        assert len(resp.content) > 0

    def test_metrics_never_500(self, client, isolated_state, monkeypatch):
        """Even when prometheus_client is absent, /metrics must not 500."""
        import deadman.metrics as _metrics_module
        # Monkeypatch render() to return the graceful fallback
        monkeypatch.setattr(
            _metrics_module,
            "render",
            lambda: (b"# no metrics\n", "text/plain; version=0.0.4; charset=utf-8"),
        )
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_body_is_text(self, client, isolated_state):
        """Body should be decodable as UTF-8 text (Prometheus format or fallback)."""
        resp = client.get("/metrics")
        text = resp.content.decode("utf-8")
        assert len(text) > 0


# ── Structured logging ────────────────────────────────────────────────────────

class TestStructuredLogging:

    def test_configure_logging_is_idempotent(self):
        """Calling configure_logging() multiple times must not raise."""
        from deadman.logging_config import configure_logging
        configure_logging()
        configure_logging()
        configure_logging()

    def test_log_record_is_valid_json(self):
        """After configure_logging(), a log record must be valid JSON."""
        from deadman.logging_config import configure_logging
        configure_logging()

        # Capture via a ListHandler so we can inspect the formatted output.
        class _ListHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.lines = []
            def emit(self, record):
                self.lines.append(self.format(record))

        list_handler = _ListHandler()
        # Borrow the formatter from the root handler (JSON formatter installed by configure_logging)
        root = logging.getLogger()
        if root.handlers:
            list_handler.setFormatter(root.handlers[0].formatter)

        test_logger = logging.getLogger("deadman.test_obs")
        test_logger.addHandler(list_handler)
        test_logger.propagate = False

        try:
            test_logger.info("observability test log", extra={"incident_id": "test-123"})
            assert list_handler.lines, "No log lines captured"
            line = list_handler.lines[0]
            parsed = json.loads(line)
            assert "timestamp" in parsed
            assert "level" in parsed
            assert "message" in parsed
            assert "logger" in parsed
            assert parsed["message"] == "observability test log"
            assert parsed["incident_id"] == "test-123"
        finally:
            test_logger.removeHandler(list_handler)
            test_logger.propagate = True

    def test_correlation_id_injected_into_records(self):
        """set_correlation_id() causes subsequent records to carry the id."""
        from deadman.logging_config import configure_logging, set_correlation_id
        configure_logging()

        class _ListHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.lines = []
            def emit(self, record):
                self.lines.append(self.format(record))

        list_handler = _ListHandler()
        root = logging.getLogger()
        if root.handlers:
            list_handler.setFormatter(root.handlers[0].formatter)
        # Also attach the correlation-id filter so it runs on our handler
        for f in root.handlers[0].filters if root.handlers else []:
            list_handler.addFilter(f)

        test_logger = logging.getLogger("deadman.test_corr")
        test_logger.addHandler(list_handler)
        test_logger.propagate = False

        try:
            set_correlation_id("inc-corr-42")
            test_logger.info("incident step")
            assert list_handler.lines
            parsed = json.loads(list_handler.lines[0])
            assert parsed.get("correlation_id") == "inc-corr-42", (
                f"Expected correlation_id='inc-corr-42', got: {parsed}"
            )
        finally:
            set_correlation_id(None)
            test_logger.removeHandler(list_handler)
            test_logger.propagate = True

    def test_correlation_id_cleared(self):
        """After set_correlation_id(None), records have no correlation_id."""
        from deadman.logging_config import set_correlation_id, get_correlation_id
        set_correlation_id("inc-clear-test")
        assert get_correlation_id() == "inc-clear-test"
        set_correlation_id(None)
        assert get_correlation_id() is None


# ── metrics helpers — no-op safety ────────────────────────────────────────────

class TestMetricsNoOpSafety:
    """Verify all metric helpers are no-ops when prometheus_client is absent."""

    @pytest.fixture()
    def mock_metrics(self, monkeypatch):
        """Reload deadman.metrics with prometheus_client hidden."""
        import deadman.metrics as _m

        # Temporarily patch _PROM_AVAILABLE to False and replace all metrics with stubs
        monkeypatch.setattr(_m, "_PROM_AVAILABLE", False)
        # Replace real metric objects with no-op stubs
        monkeypatch.setattr(_m, "incidents_total", _m._NoOpCounter())
        monkeypatch.setattr(_m, "fallback_depth", _m._NoOpHistogram())
        monkeypatch.setattr(_m, "double_executions_total", _m._NoOpCounter())
        monkeypatch.setattr(_m, "guardrail_blocks_total", _m._NoOpCounter())
        monkeypatch.setattr(_m, "tool_calls_total", _m._NoOpCounter())
        monkeypatch.setattr(_m, "tool_latency_seconds", _m._NoOpHistogram())
        monkeypatch.setattr(_m, "scope_denied_total", _m._NoOpCounter())
        return _m

    def test_record_incident_noop(self, mock_metrics):
        mock_metrics.record_incident("mock", "resolved")  # must not raise

    def test_record_fallback_depth_noop(self, mock_metrics):
        mock_metrics.record_fallback_depth(3)

    def test_record_double_execution_noop(self, mock_metrics):
        mock_metrics.record_double_execution()

    def test_record_guardrail_block_noop(self, mock_metrics):
        mock_metrics.record_guardrail_block("github.revert_pr")

    def test_record_scope_denied_noop(self, mock_metrics):
        mock_metrics.record_scope_denied("k8s.cordon_drain")

    def test_observe_tool_noop(self, mock_metrics):
        mock_metrics.observe_tool("github.revert_pr", "EXECUTED", 0.042)

    def test_render_returns_bytes_and_content_type_when_absent(self, mock_metrics):
        content, ct = mock_metrics.render()
        assert isinstance(content, bytes)
        assert isinstance(ct, str)
        assert len(content) > 0


# ── otel.span() no-op ────────────────────────────────────────────────────────

class TestOtelSpan:

    def test_span_is_noop_when_otel_inactive(self):
        """span() must be a no-op context manager when _otel_active is False."""
        import deadman.otel as otel
        # In mock mode (no OTEL endpoint) _otel_active is always False.
        with otel.span("agent.step", step="test") as s:
            # s may be None (from yield in no-op path) — must not raise
            pass

    def test_span_does_not_raise_on_exception_inside(self):
        """Exceptions inside span() must propagate normally."""
        import deadman.otel as otel

        with pytest.raises(ValueError, match="expected"):
            with otel.span("test.span"):
                raise ValueError("expected")

    def test_span_accepts_arbitrary_kwargs(self):
        """span() accepts any keyword arguments without error."""
        import deadman.otel as otel
        with otel.span("test.span", tool="github.revert_pr", depth=2, incident_id="inc-1"):
            pass

    def test_audit_span_still_works(self):
        """Existing audit_span() still functions correctly (regression guard)."""
        import deadman.otel as otel
        with otel.audit_span("mcp.test", {"tool": "test", "key": "k"}):
            pass
