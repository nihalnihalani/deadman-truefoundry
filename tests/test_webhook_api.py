"""Tests 10: Webhook API (TestClient).

/healthz shape, /incident, /incident/{id}/postmortem, /api/demo/run headline
(naive=1, deadman=0), /api/demo/stream?fast=1 emits beats and final done event,
/api/chaos/{toggle} for ALL toggles incl. kill_mid_rollback and reset.
"""
from __future__ import annotations
import json
import pytest

from fastapi.testclient import TestClient
from deadman.webhook import app

client = TestClient(app)


class TestHealthz:

    def test_healthz_returns_ok(self, isolated_state):
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_shape(self, isolated_state):
        data = client.get("/healthz").json()
        assert "ok" in data
        assert "mode" in data
        assert data["ok"] is True

    def test_healthz_mode_is_mock(self, isolated_state):
        """In test environment MODE is 'mock'."""
        data = client.get("/healthz").json()
        assert data["mode"] == "mock"


class TestIncidentEndpoint:

    def test_post_incident_returns_scoreboard(self, isolated_state):
        resp = client.post("/incident", json={"incident_id": "inc-test-1", "summary": "test"})
        assert resp.status_code == 200

    def test_incident_scoreboard_shape(self, isolated_state):
        resp = client.post("/incident", json={"incident_id": "inc-test-2"})
        data = resp.json()
        required_keys = {"survived", "backend", "fallback_depth", "double_executions",
                         "guardrail_blocks", "drain_authority", "timeline", "mode", "incident_id"}
        assert required_keys.issubset(data.keys()), f"Missing keys: {required_keys - set(data.keys())}"

    def test_incident_survived_true(self, isolated_state):
        resp = client.post("/incident", json={"incident_id": "inc-test-3"})
        assert resp.json()["survived"] is True

    def test_incident_double_executions_zero(self, isolated_state):
        """No chaos -> no double executions."""
        resp = client.post("/incident", json={"incident_id": "inc-test-4"})
        assert resp.json()["double_executions"] == 0


class TestPostmortemEndpoint:

    def test_postmortem_returns_audit_trail(self, isolated_state):
        inc_id = "pm-test-1"
        client.post("/incident", json={"incident_id": inc_id})
        resp = client.get(f"/incident/{inc_id}/postmortem")
        assert resp.status_code == 200

    def test_postmortem_shape(self, isolated_state):
        inc_id = "pm-test-2"
        client.post("/incident", json={"incident_id": inc_id})
        data = client.get(f"/incident/{inc_id}/postmortem").json()
        assert "incident_id" in data
        assert "audit_trail" in data
        assert data["incident_id"] == inc_id
        assert isinstance(data["audit_trail"], list)

    def test_postmortem_has_entries_after_incident(self, isolated_state):
        """Running an incident populates the audit trail."""
        inc_id = "pm-test-3"
        client.post("/incident", json={"incident_id": inc_id})
        data = client.get(f"/incident/{inc_id}/postmortem").json()
        assert len(data["audit_trail"]) > 0

    def test_postmortem_unknown_incident_empty_trail(self, isolated_state):
        """Querying postmortem for a non-existent incident returns empty trail."""
        resp = client.get("/incident/nonexistent-xyz-999/postmortem")
        assert resp.status_code == 200
        data = resp.json()
        assert data["audit_trail"] == []


class TestDemoRun:

    def test_demo_run_returns_200(self, isolated_state):
        resp = client.post("/api/demo/run")
        assert resp.status_code == 200

    def test_demo_run_shape(self, isolated_state):
        data = client.post("/api/demo/run").json()
        assert "naive" in data
        assert "deadman" in data
        assert "headline" in data

    def test_demo_run_headline_naive_double_executes(self, isolated_state):
        data = client.post("/api/demo/run").json()
        headline = data["headline"]
        assert "double_executions_naive" in headline
        assert "double_executions_deadman" in headline
        assert headline["double_executions_naive"] >= 1, (
            f"Naive should double-execute in demo, got {headline['double_executions_naive']}"
        )

    def test_demo_run_headline_deadman_zero(self, isolated_state):
        data = client.post("/api/demo/run").json()
        headline = data["headline"]
        assert headline["double_executions_deadman"] == 0, (
            f"Deadman should have 0 double executions, got {headline['double_executions_deadman']}"
        )

    def test_demo_run_scoreboard_both_fields(self, isolated_state):
        data = client.post("/api/demo/run").json()
        for side in ("naive", "deadman"):
            sb = data[side]
            assert "survived" in sb
            assert "double_executions" in sb
            assert "fallback_depth" in sb


class TestDemoStream:

    def _collect_sse_events(self, url: str) -> list[dict]:
        """Collect all SSE data: events from the stream."""
        events = []
        with client.stream("GET", url) as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    events.append(payload)
        return events

    def test_stream_emits_events(self, isolated_state):
        """fast=1 stream emits at least one SSE event."""
        events = self._collect_sse_events("/api/demo/stream?fast=1")
        assert len(events) > 0

    def test_stream_has_done_event(self, isolated_state):
        """Stream ends with a 'done' beat event."""
        events = self._collect_sse_events("/api/demo/stream?fast=1")
        beats = [e.get("beat") for e in events]
        assert "done" in beats, f"No 'done' beat found. Beats: {beats}"

    def test_stream_done_event_has_headline(self, isolated_state):
        """The 'done' event carries headline with double_executions counts."""
        events = self._collect_sse_events("/api/demo/stream?fast=1")
        done_events = [e for e in events if e.get("beat") == "done"]
        assert done_events
        done = done_events[-1]
        assert "headline" in done, f"Done event missing headline: {done}"
        assert "double_executions_naive" in done["headline"]
        assert "double_executions_deadman" in done["headline"]

    def test_stream_done_event_has_scoreboard(self, isolated_state):
        """The 'done' event carries final scoreboards for both agents."""
        events = self._collect_sse_events("/api/demo/stream?fast=1")
        done_events = [e for e in events if e.get("beat") == "done"]
        assert done_events
        done = done_events[-1]
        assert "scoreboard" in done
        assert "naive" in done["scoreboard"]
        assert "deadman" in done["scoreboard"]

    def test_stream_events_have_required_fields(self, isolated_state):
        """Every SSE event (except done) has t, beat, side, scoreboard, note fields."""
        events = self._collect_sse_events("/api/demo/stream?fast=1")
        for ev in events:
            if ev.get("beat") == "done":
                continue
            assert "t" in ev, f"Missing 't' in event: {ev}"
            assert "beat" in ev, f"Missing 'beat' in event: {ev}"
            assert "side" in ev, f"Missing 'side' in event: {ev}"

    def test_stream_first_beat_is_cold_open(self, isolated_state):
        """First SSE beat is 'cold_open'."""
        events = self._collect_sse_events("/api/demo/stream?fast=1")
        assert events[0]["beat"] == "cold_open"


class TestChaoToggle:

    def test_correlated_blackout_toggle(self, isolated_state):
        resp = client.post("/api/chaos/correlated_blackout")
        assert resp.status_code == 200
        data = resp.json()
        assert data["toggle"] == "correlated_blackout"
        assert data["chaos"]["correlated_blackout"] is True

    def test_rate_limit_storm_toggle(self, isolated_state):
        resp = client.post("/api/chaos/rate_limit_storm")
        assert resp.status_code == 200
        assert resp.json()["chaos"]["rate_limit_storm"] is True

    def test_kill_bedrock_toggle(self, isolated_state):
        resp = client.post("/api/chaos/kill_bedrock")
        assert resp.status_code == 200
        assert resp.json()["chaos"]["kill_bedrock"] is True

    def test_corrupt_output_toggle(self, isolated_state):
        resp = client.post("/api/chaos/corrupt_output")
        assert resp.status_code == 200
        assert resp.json()["chaos"]["corrupt_output"] is True

    def test_kill_mid_rollback_toggle(self, isolated_state):
        resp = client.post("/api/chaos/kill_mid_rollback")
        assert resp.status_code == 200
        data = resp.json()
        assert data["chaos"]["kill_mid_rollback"] is True

    def test_reset_toggle_clears_state(self, isolated_state):
        """Toggling correlated_blackout then reset clears it."""
        client.post("/api/chaos/correlated_blackout")
        resp = client.post("/api/chaos/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["chaos"]["correlated_blackout"] is False
        assert data["chaos"]["kill_bedrock"] is False
        assert data["chaos"]["corrupt_output"] is False
        assert data["chaos"]["kill_mid_rollback"] is False

    def test_unknown_toggle_returns_400(self, isolated_state):
        resp = client.post("/api/chaos/nonexistent_toggle")
        assert resp.status_code == 400

    def test_all_valid_toggles_accepted(self, isolated_state):
        """Every documented toggle is accepted by the endpoint."""
        valid_toggles = ["correlated_blackout", "rate_limit_storm", "kill_bedrock",
                         "corrupt_output", "kill_mid_rollback", "reset"]
        for toggle in valid_toggles:
            resp = client.post(f"/api/chaos/{toggle}")
            assert resp.status_code == 200, f"Toggle '{toggle}' rejected: {resp.text}"
