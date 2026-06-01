"""System-of-record adapters for DEADMAN.

Two implementations:

  World     — in-memory mock for demo/test. Deliberately NOT naturally idempotent:
              calling revert_pr twice causes two entries (the double-execution hazard
              the demo highlights). DO NOT change its semantics; prove_exactly_once.py
              and run_demo.py both depend on this behavior.

  RealWorld — production adapter. Side effects flow through the MCP Gateway (which
              routes via realmode_mcp.py → TrueFoundry MCP Gateway → real tools with
              Cedar guardrails + idempotency). RealWorld records *intent* locally and
              exposes system-of-record query methods backed by the durable audit log.
              It does NOT re-execute the side effect — that is the MCPGateway's job.

How real side effects flow
--------------------------
1. commander.py calls MCPGateway.execute(tool, args, key, scope).
2. MCPGateway (real mode) calls realmode_mcp.call_tool(tool, args, key) which sends
   the request to the TrueFoundry MCP Gateway with an Idempotency-Key header.
3. The TFY MCP Gateway enforces Cedar guardrails, deduplicates on the key, routes to
   the real tool server (GitHub API, k8s, etc.), and returns the result.
4. MCPGateway writes a COMMITTED record to the AuditLog.
5. RealWorld.is_reverted() / is_cordoned() query the AuditLog for COMMITTED records
   rather than calling the live system again — this is safe because MCPGateway only
   writes COMMITTED after a confirmed successful tool execution.

This means RealWorld is *not* a bypass of the gateway — it is a thin query layer over
the audit log that the commander uses for reconciliation (resume path) without making
extra live API calls.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Mock World (demo / test — keep EXACTLY as is)
# ---------------------------------------------------------------------------

class World:
    """In-memory mock system of record.

    revert_pr is deliberately NOT naturally idempotent — calling it twice records
    two entries. That is the double-execution hazard the exactly-once proof isolates.
    """

    def __init__(self):
        self.applied: list[tuple] = []   # every side effect that actually hit prod

    def revert_pr(self, pr: str, key: str | None = None):
        self.applied.append(("revert_pr", pr, key))

    def cordon_drain(self, node: str, key: str | None = None):
        self.applied.append(("cordon_drain", node, key))

    def asg_scale(self, asg: str, replicas: int, key: str | None = None):
        self.applied.append(("asg_scale", asg, replicas, key))

    # system-of-record queries DEADMAN uses to verify before re-acting
    def is_reverted(self, pr: str) -> bool:
        return any(a == "revert_pr" and p == pr for (a, p, *_rest) in self.applied)

    def is_cordoned(self, node: str) -> bool:
        return any(a == "cordon_drain" and n == node for (a, n, *_rest) in self.applied)

    def is_scaled(self, asg: str) -> bool:
        return any(a == "asg_scale" and x == asg for (a, x, *_rest) in self.applied)

    def count(self, action: str) -> int:
        return sum(1 for rec in self.applied if rec[0] == action)


# ---------------------------------------------------------------------------
# RealWorld — production adapter
# ---------------------------------------------------------------------------

class RealWorld:
    """Production system-of-record adapter for use with MCPGateway in real mode.

    Side effects (revert_pr, cordon_drain, asg_scale) are NOT executed here — they
    flow through MCPGateway → realmode_mcp → TrueFoundry MCP Gateway → real tools.
    RealWorld's role is:
      1. Accept the *intent* record from MCPGateway after a confirmed side effect
         (MCPGateway calls world.revert_pr(pr, key) after the tool call succeeds).
      2. Provide is_reverted() / count() / is_cordoned() reconciliation queries with a
         three-layer escalation:
           a. in-memory intent log (same-process, before audit flush);
           b. durable AuditLog COMMITTED records (cross-process resume);
           c. a READ-ONLY LIVE query to the actual system of record via the MCP Gateway,
              when (a) and (b) are inconclusive AND config.TFY_MCP_GATEWAY_URL is set.
              This makes "reconcile against the system-of-record before re-acting" REAL
              for production rather than a hollow audit-log proxy.

    Read-only MCP tools expected on the gateway (no side effects, idempotency_key prefixed
    "readcheck::" so the gateway treats them as safe reads):
      • "github.get_pr_state" {"pr": <pr>}  -> body indicating revert/merge state.
          We treat the PR as reverted when the response body reports any of:
          body["reverted"] is True, body["state"] in {"reverted","rolled_back"}, or
          body["revert_pr"] (a revert PR number) is present.
      • "k8s.describe" {"node": <node>}  -> body indicating cordon/drain state.
          We treat the node as cordoned when body["cordoned"]/body["unschedulable"] is True
          or body["spec"]["unschedulable"] is True.

    Usage (webhook / production path):
        audit = AuditLog(incident_id)
        world = RealWorld(audit_log=audit)
        mcp   = MCPGateway(world, audit, chaos=None)
        agent = Deadman(incident_id, world, chaos=None)

    The MCPGateway calls world.revert_pr() AFTER the real tool call succeeds (same as
    mock World). RealWorld records it so commander.py's is_reverted() reconciliation
    works correctly on the resume path even before the audit log flush completes.
    """

    def __init__(self, audit_log=None):
        """
        Parameters
        ----------
        audit_log : AuditLog | None
            Optional durable audit log. When provided, is_reverted() and is_cordoned()
            cross-check against COMMITTED audit records in addition to the in-memory log.
            This allows the commander's reconciliation path to work correctly across
            process restarts (the in-memory log is empty after a restart, but the audit
            log persists).
        """
        self._applied: list[tuple] = []
        self._audit_log = audit_log

    # ---- intent recording (called by MCPGateway after real tool call succeeds) ----

    def revert_pr(self, pr: str, key: str | None = None):
        """Record that pr was reverted via the MCP Gateway."""
        self._applied.append(("revert_pr", pr, key))

    def cordon_drain(self, node: str, key: str | None = None):
        """Record that node was cordoned/drained via the MCP Gateway."""
        self._applied.append(("cordon_drain", node, key))

    def asg_scale(self, asg: str, replicas: int, key: str | None = None):
        """Record that asg was scaled via the MCP Gateway."""
        self._applied.append(("asg_scale", asg, replicas, key))

    # ---- system-of-record queries ----

    def is_reverted(self, pr: str) -> bool:
        """Return True if pr was reverted — in-memory, audit log, or LIVE system of record."""
        # (a) In-memory check (same-process path and resume before log flush)
        if any(a == "revert_pr" and p == pr for (a, p, *_rest) in self._applied):
            return True
        # (b) Durable audit log check (cross-process resume)
        if self._audit_log is not None:
            for e in self._audit_log._entries():
                if (
                    e.get("status") == "COMMITTED"
                    and e.get("tool") == "github.revert_pr"
                    and pr in e.get("key", "")
                ):
                    return True
        # (c) LIVE read-only query against the real system of record via the MCP Gateway.
        body = self._live_query("github.get_pr_state", {"pr": pr}, "readcheck::" + pr)
        if isinstance(body, dict):
            if body.get("reverted") is True:
                return True
            if body.get("state") in ("reverted", "rolled_back"):
                return True
            if body.get("revert_pr"):
                return True
        return False

    def is_cordoned(self, node: str) -> bool:
        """Return True if node was cordoned — in-memory, audit log, or LIVE system of record."""
        # (a) In-memory check
        if any(a == "cordon_drain" and n == node for (a, n, *_rest) in self._applied):
            return True
        # (b) Durable audit log check
        if self._audit_log is not None:
            for e in self._audit_log._entries():
                if (
                    e.get("status") == "COMMITTED"
                    and e.get("tool") == "k8s.cordon_drain"
                    and node in e.get("key", "")
                ):
                    return True
        # (c) LIVE read-only query against the real cluster via the MCP Gateway.
        body = self._live_query("k8s.describe", {"node": node}, "readcheck::" + node)
        if isinstance(body, dict):
            if body.get("cordoned") is True or body.get("unschedulable") is True:
                return True
            spec = body.get("spec")
            if isinstance(spec, dict) and spec.get("unschedulable") is True:
                return True
        return False

    def is_scaled(self, asg: str) -> bool:
        """Return True if asg was scaled — in-memory or durable audit log."""
        if any(a == "asg_scale" and x == asg for (a, x, *_rest) in self._applied):
            return True
        if self._audit_log is not None:
            for e in self._audit_log._entries():
                if (
                    e.get("status") == "COMMITTED"
                    and e.get("tool") == "asg.scale"
                    and asg in e.get("key", "")
                ):
                    return True
        return False

    def _live_query(self, tool: str, args: dict, idempotency_key: str):
        """Issue a READ-ONLY MCP gateway query; return the parsed body or None.

        Only fires when config.TFY_MCP_GATEWAY_URL is configured. Any failure (no gateway,
        network error, gateway error, unparseable body) falls back to None so callers keep
        their prior audit-log behavior. The idempotency_key is "readcheck::"-prefixed so the
        gateway treats this as a safe, replay-tolerant read with no side effects.
        """
        import deadman.config as config
        if not config.TFY_MCP_GATEWAY_URL:
            return None
        try:
            from deadman import realmode_mcp
            result = realmode_mcp.call_tool(tool, args, idempotency_key=idempotency_key)
            return result.get("body") if isinstance(result, dict) else None
        except Exception:
            # Live query unavailable/failed -> inconclusive; caller already tried audit log.
            return None

    def count(self, action: str) -> int:
        """Count in-memory intent records for the given action.

        Note: for production correctness use the audit log's postmortem() which reflects
        the durable committed count. This in-memory count is provided for API compatibility
        with the mock World and is accurate within a single process lifetime.
        """
        return sum(1 for rec in self._applied if rec[0] == action)
