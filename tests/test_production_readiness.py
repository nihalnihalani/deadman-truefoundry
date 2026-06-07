"""Production-readiness boundaries for the HTTP entrypoint and config."""
from __future__ import annotations

from fastapi.testclient import TestClient

import deadman.config as config
from deadman.webhook import app


client = TestClient(app)


def _set_real_gateway_config(monkeypatch):
    monkeypatch.setattr(config, "MODE", "real")
    monkeypatch.setattr(config, "TFY_API_KEY", "test-key")
    monkeypatch.setattr(config, "TFY_GATEWAY_BASE_URL", "https://tfy.example.test/llm")
    monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://tfy.example.test/mcp")
    monkeypatch.setattr(config, "STATE_BACKEND", "file")


class TestReadiness:

    def test_mock_readyz_is_ok(self, isolated_state, monkeypatch):
        monkeypatch.setattr(config, "MODE", "mock")
        monkeypatch.delenv("DEADMAN_ENABLE_DEMO", raising=False)

        resp = client.get("/readyz")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["mode"] == "mock"
        assert data["demo_enabled"] is True

    def test_real_readyz_fails_without_webhook_secret(self, isolated_state, monkeypatch):
        _set_real_gateway_config(monkeypatch)
        monkeypatch.delenv("DEADMAN_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("DEADMAN_ENABLE_DEMO", raising=False)

        resp = client.get("/readyz")

        assert resp.status_code == 503
        fields = {issue["field"] for issue in resp.json()["errors"]}
        assert "DEADMAN_WEBHOOK_SECRET" in fields

    def test_real_readyz_rejects_enabled_demo_endpoints(self, isolated_state, monkeypatch):
        _set_real_gateway_config(monkeypatch)
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", "secret")
        monkeypatch.setenv("DEADMAN_ENABLE_DEMO", "1")

        resp = client.get("/readyz")

        assert resp.status_code == 503
        fields = {issue["field"] for issue in resp.json()["errors"]}
        assert "DEADMAN_ENABLE_DEMO" in fields

    def test_real_readyz_ok_with_safe_minimum_config(self, isolated_state, monkeypatch):
        _set_real_gateway_config(monkeypatch)
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", "secret")
        monkeypatch.delenv("DEADMAN_ENABLE_DEMO", raising=False)

        resp = client.get("/readyz")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["demo_enabled"] is False
        assert any(issue["field"] == "DEADMAN_STATE_BACKEND" for issue in data["warnings"])

    def test_readyz_rejects_invalid_mcp_transport(self, isolated_state, monkeypatch):
        _set_real_gateway_config(monkeypatch)
        monkeypatch.setattr(config, "TFY_MCP_TRANSPORT", "bogus")
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", "secret")

        resp = client.get("/readyz")

        assert resp.status_code == 503
        fields = {issue["field"] for issue in resp.json()["errors"]}
        assert "TFY_MCP_TRANSPORT" in fields


class TestFailClosedHttp:

    def test_real_incident_without_secret_fails_closed(self, isolated_state, monkeypatch):
        _set_real_gateway_config(monkeypatch)
        monkeypatch.delenv("DEADMAN_WEBHOOK_SECRET", raising=False)

        resp = client.post("/incident", json={"incident_id": "prod-incident-1"})

        assert resp.status_code == 503
        assert "DEADMAN_WEBHOOK_SECRET" in resp.text

    def test_demo_endpoints_disabled_by_default_in_real_mode(self, isolated_state, monkeypatch):
        _set_real_gateway_config(monkeypatch)
        monkeypatch.delenv("DEADMAN_ENABLE_DEMO", raising=False)

        resp = client.post("/api/demo/run")

        assert resp.status_code == 404

    def test_invalid_incident_id_rejected_before_file_access(self, isolated_state):
        resp = client.post("/incident", json={"incident_id": "../escape", "summary": "bad"})

        assert resp.status_code == 422
        assert "incident_id" in resp.text

    def test_invalid_postmortem_id_rejected_before_file_access(self, isolated_state):
        resp = client.get("/incident/bad%20id/postmortem")

        assert resp.status_code == 422
        assert "incident_id" in resp.text
