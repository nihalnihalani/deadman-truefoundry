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

    class _FakeDeadman:
        def __init__(self, incident_id, world, chaos=None):
            captured["world"] = world
            captured["incident_id"] = incident_id

        def run(self, resume=False):
            captured["resume"] = resume

            class _SB:  # minimal scoreboard stand-in
                survived = True; backend = "tier-0"; fallback_depth = 0
                double_executions = 0; guardrail_blocks = 0
                drain_authority = "ON"; notes: list = []
            return _SB()

    monkeypatch.setattr(wh, "Deadman", _FakeDeadman)

    # 1) Real mode, no prior state -> RealWorld, fresh run (resume=False).
    wh._run_incident("inc-real-fresh")
    assert isinstance(captured["world"], RealWorld), "real mode must use RealWorld, not mock World"
    assert captured["resume"] is False

    # 2) Real mode WITH a pending durable action -> resume=True (process-death recovery).
    ds = DurableState("inc-real-pending")
    ds.set_pending("github.revert_pr", "inc-real-pending::revert_pr::PR-1")
    wh._run_incident("inc-real-pending")
    assert isinstance(captured["world"], RealWorld)
    assert captured["resume"] is True, "a pending uncommitted action must trigger resume, not a fresh run"

    # 3) Mock mode still uses the in-memory World.
    monkeypatch.setattr(config, "MODE", "mock")
    wh._run_incident("inc-mock-fresh")
    assert type(captured["world"]) is World


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
