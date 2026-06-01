"""Regression tests for the real-mode exactly-once hardening (Raven red-team P0-2/P0-3).

These lock in the fixes that make "exactly-once across process death" honest in production:

  * P0-2 — a truly fresh process (empty in-memory World, audit showing PENDING-not-COMMITTED)
           must reconcile against the LIVE system of record before re-acting, not against a
           hollow audit-log proxy. RealWorld.is_reverted/is_cordoned escalate to a read-only
           MCP query when a gateway is configured.
  * P0-3 — AuditLog.claim_commit is an atomic, race-safe COMMIT that returns False on replay,
           so two workers cannot both record success for the same idempotency key.
"""
from __future__ import annotations

import deadman.config as config
from deadman.world import RealWorld
from deadman.state import AuditLog


class _PendingOnlyAudit:
    """Stand-in audit log: the dangerous window — PENDING written, COMMIT lost to the crash."""

    def _entries(self):
        return [{
            "status": "PENDING",
            "tool": "github.revert_pr",
            "key": "incident-42::revert_pr::PR-1337",
        }]


def test_fresh_process_reconciles_via_live_system_of_record(monkeypatch):
    """P0-2: empty memory + PENDING-only audit + live provider says reverted -> no re-action."""
    monkeypatch.setattr(config, "MODE", "real")
    monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://mcp.example.test")

    import deadman.realmode_mcp as rmcp
    calls = {"n": 0}

    def fake_call_tool(tool, args, idempotency_key):
        calls["n"] += 1
        if tool == "github.get_pr_state":
            return {"status_code": 200,
                    "body": {"pr": args["pr"], "reverted": True, "state": "reverted"},
                    "skipped_idempotent": False}
        return {"status_code": 200, "body": {}, "skipped_idempotent": False}

    monkeypatch.setattr(rmcp, "call_tool", fake_call_tool)

    rw = RealWorld(audit_log=_PendingOnlyAudit())
    assert rw.is_reverted("PR-1337") is True       # reconciled via the live query
    assert calls["n"] >= 1                          # a real live query was issued (not hollow)


def test_fresh_process_acts_when_provider_says_not_reverted(monkeypatch):
    """Negative control: provider says NOT reverted -> agent is correctly allowed to act."""
    monkeypatch.setattr(config, "MODE", "real")
    monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://mcp.example.test")

    import deadman.realmode_mcp as rmcp

    def fake_call_tool(tool, args, idempotency_key):
        return {"status_code": 200,
                "body": {"pr": args["pr"], "reverted": False, "state": "open"},
                "skipped_idempotent": False}

    monkeypatch.setattr(rmcp, "call_tool", fake_call_tool)

    rw = RealWorld(audit_log=_PendingOnlyAudit())
    assert rw.is_reverted("PR-1337") is False


def test_live_query_failure_falls_back_to_audit(monkeypatch):
    """A failing/unavailable live query must not crash — falls back to the audit-log check."""
    monkeypatch.setattr(config, "MODE", "real")
    monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://mcp.example.test")

    import deadman.realmode_mcp as rmcp

    def boom(tool, args, idempotency_key):
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(rmcp, "call_tool", boom)

    # Audit log has no COMMITTED record -> falls back to False (does not raise).
    rw = RealWorld(audit_log=_PendingOnlyAudit())
    assert rw.is_reverted("PR-1337") is False


def test_webhook_wires_realworld_and_resumes_in_real_mode(monkeypatch, tmp_path):
    """NEW-1 (Raven final pass): the /incident entrypoint must construct RealWorld in real
    mode (so live reconciliation actually runs in the server) and resume when durable state
    holds a pending action (so a re-delivered alert can't double-execute the in-flight action).
    """
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MODE", "real")

    import deadman.webhook as wh
    from deadman.world import RealWorld, World
    from deadman.state import DurableState

    captured = {}

    class _SB:  # minimal scoreboard stand-in
        survived = True; backend = "tier-0"; fallback_depth = 0
        double_executions = 0; guardrail_blocks = 0
        drain_authority = "ON"; notes: list = []

    class _FakeDeadman:
        def __init__(self, incident_id, world, chaos=None):
            captured["world"] = world
            captured["incident_id"] = incident_id

        def run(self, resume=False):
            captured["method"] = "run"
            captured["resume"] = resume
            return _SB()

        def run_agentic(self, summary, max_steps=8, resume=False):
            captured["method"] = "run_agentic"
            captured["summary"] = summary
            captured["resume"] = resume
            return _SB()

    monkeypatch.setattr(wh, "Deadman", _FakeDeadman)

    # 1) Real mode, no prior state -> RealWorld + the LLM-driven agentic loop, summary fed in.
    wh._run_incident("inc-real-fresh", "payments-db latency spike")
    assert isinstance(captured["world"], RealWorld), "real mode must use RealWorld, not mock World"
    assert captured["method"] == "run_agentic", "real mode must drive the agentic loop, not scripted run()"
    assert captured["summary"] == "payments-db latency spike", "the alert summary must reach the agent"
    assert captured["resume"] is False

    # 2) Real mode WITH a pending durable action -> resume=True (process-death recovery).
    ds = DurableState("inc-real-pending")
    ds.set_pending("github.revert_pr", "inc-real-pending::revert_pr::PR-1")
    wh._run_incident("inc-real-pending", "deploy regression")
    assert isinstance(captured["world"], RealWorld)
    assert captured["method"] == "run_agentic"
    assert captured["resume"] is True, "a pending uncommitted action must trigger resume, not a fresh run"

    # 3) Mock mode still uses the in-memory World + the deterministic scripted run().
    monkeypatch.setattr(config, "MODE", "mock")
    wh._run_incident("inc-mock-fresh", "ignored in mock")
    assert type(captured["world"]) is World
    assert captured["method"] == "run", "mock mode keeps the deterministic demo path"


def test_realmode_incident_runs_agentic_loop_end_to_end(monkeypatch, tmp_path):
    """P1-2 (Raven iter-2): real-mode POST /incident must drive the genuine LLM agentic loop
    (model chooses tools), feed the alert summary in, route tools through the MCP gateway,
    and require the webhook secret — all with no live network.
    """
    monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(config, "MODE", "real")
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://mcp.test")

    import importlib
    import deadman.webhook as wh
    importlib.reload(wh)  # pick up the patched env/config for module-level flags

    import deadman.ai_gateway as aig
    from deadman.ai_gateway import Completion

    turns = iter([
        '{"tool":"cw.get_metrics","args":{},"rationale":"triage","done":false}',
        '{"tool":"github.revert_pr","args":{"pr":"PR-77"},"rationale":"rollback","done":false}',
        '{"done":true,"rationale":"resolved"}',
    ])

    def fake_complete(self, prompt):
        try:
            txt = next(turns)
        except StopIteration:
            txt = '{"done":true}'
        return Completion(txt, "claude-opus-4-8@us-east-1", 0, False)

    monkeypatch.setattr(aig.AIGateway, "complete", fake_complete)

    import deadman.realmode_mcp as rmcp
    calls = []

    def fake_call_tool(tool, args, idempotency_key):
        calls.append(tool)
        return {"status_code": 200, "body": {"ok": True, "reverted": True}, "skipped_idempotent": False}

    monkeypatch.setattr(rmcp, "call_tool", fake_call_tool)

    from fastapi.testclient import TestClient
    with TestClient(wh.app) as c:
        # secret is configured -> an unauthenticated request is rejected (401)
        assert c.post("/incident", json={"incident_id": "i-noauth", "summary": "x"}).status_code == 401
        # with the secret -> the agentic loop runs
        r = c.post(
            "/incident",
            json={"incident_id": "i-agentic", "summary": "payments-db latency spike"},
            headers={"Authorization": "Bearer test-secret"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "real" and body["survived"] is True
    # the LLM's chosen tools were actually routed through the MCP gateway (no live network)
    assert any("revert" in t for t in calls), f"agentic loop did not drive a tool call: {calls}"


def test_claim_commit_is_atomic_and_idempotent(monkeypatch, tmp_path):
    """P0-3: claim_commit wins exactly once for a key; replays return False (no double-commit)."""
    monkeypatch.setattr(config, "MODE", "mock")
    monkeypatch.setattr(config, "STATE_BACKEND", "file")
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))

    audit = AuditLog("inc-claim-test")
    key = "inc-claim-test::revert_pr::PR-9"

    assert audit.claim_commit(key, "github.revert_pr") is True    # first caller wins
    assert audit.claim_commit(key, "github.revert_pr") is False   # replay loses
    assert audit.claim_commit(key, "github.revert_pr") is False   # still loses
    assert audit.is_committed(key) is True

    # Exactly one COMMITTED record exists for the key.
    committed = [e for e in audit._entries()
                 if e.get("status") == "COMMITTED" and e.get("key") == key]
    assert len(committed) == 1
