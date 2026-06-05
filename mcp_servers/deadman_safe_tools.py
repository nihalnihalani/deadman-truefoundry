"""Safe demo MCP server for DEADMAN real-mode setup.

This server exposes the exact tool names DEADMAN expects, but every destructive
operation is a harmless in-memory simulation. It is intended for hackathon demos
and first-time TrueFoundry MCP Gateway wiring, before connecting real GitHub,
Kubernetes, ASG, CloudWatch, and Statuspage integrations.

Hosted STDIO command:
    python mcp_servers/deadman_safe_tools.py

Local HTTP command:
    python mcp_servers/deadman_safe_tools.py --transport http --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import argparse
import time
from typing import Any

from fastmcp import FastMCP


mcp = FastMCP("DEADMAN Safe Tools")

_reverted_prs: set[str] = set()
_cordoned_nodes: set[str] = set()
_scaled_asgs: dict[str, int] = {}
_status_updates: list[dict[str, Any]] = []


@mcp.tool(name="cw.get_metrics")
def cw_get_metrics(
    metric: str = "cpu",
    window: str = "5m",
    _returns: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return safe synthetic CloudWatch-like metrics."""
    if _returns is not None:
        return _returns
    return {
        "metric": metric,
        "window": window,
        "cpu": 0.91,
        "error_rate": 0.07,
        "p99_ms": 1840,
        "region": "us-east-1",
        "source": "deadman-safe-mcp",
    }


@mcp.tool(name="logs.query")
def logs_query(query: str = "error", window: str = "5m") -> dict[str, Any]:
    """Return safe synthetic log search results."""
    return {
        "query": query,
        "window": window,
        "matches": [
            {
                "service": "checkout-api",
                "level": "error",
                "message": "deployment PR-1337 introduced elevated 5xx responses",
            }
        ],
        "source": "deadman-safe-mcp",
    }


@mcp.tool(name="k8s.describe")
def k8s_describe(node: str = "prod-node-7", namespace: str = "default") -> dict[str, Any]:
    """Describe a synthetic Kubernetes node."""
    return {
        "node": node,
        "namespace": namespace,
        "cordoned": node in _cordoned_nodes,
        "unschedulable": node in _cordoned_nodes,
        "ready": True,
        "source": "deadman-safe-mcp",
    }


@mcp.tool(name="github.get_pr_state")
def github_get_pr_state(pr: str = "PR-1337") -> dict[str, Any]:
    """Read-only synthetic PR state for resume reconciliation."""
    reverted = pr in _reverted_prs
    return {
        "pr": pr,
        "state": "reverted" if reverted else "merged",
        "reverted": reverted,
        "revert_pr": f"revert-{pr}" if reverted else None,
        "source": "deadman-safe-mcp",
    }


@mcp.tool(name="github.revert_pr")
def github_revert_pr(pr: str = "PR-1337", reason: str = "deadman rollback") -> dict[str, Any]:
    """Simulate reverting a GitHub PR without touching GitHub."""
    already_reverted = pr in _reverted_prs
    _reverted_prs.add(pr)
    return {
        "pr": pr,
        "reverted": True,
        "already_reverted": already_reverted,
        "reason": reason,
        "source": "deadman-safe-mcp",
    }


@mcp.tool(name="k8s.cordon_drain")
def k8s_cordon_drain(
    node: str = "prod-node-7",
    namespace: str = "default",
    elevation: bool = False,
) -> dict[str, Any]:
    """Simulate cordon/drain without touching a Kubernetes cluster."""
    already_cordoned = node in _cordoned_nodes
    _cordoned_nodes.add(node)
    return {
        "node": node,
        "namespace": namespace,
        "cordoned": True,
        "drained": True,
        "already_cordoned": already_cordoned,
        "elevation_used": elevation,
        "source": "deadman-safe-mcp",
    }


@mcp.tool(name="asg.scale")
def asg_scale(asg: str = "checkout-asg", replicas: int = 3) -> dict[str, Any]:
    """Simulate scaling an ASG without touching AWS."""
    previous = _scaled_asgs.get(asg)
    _scaled_asgs[asg] = replicas
    return {
        "asg": asg,
        "previous_replicas": previous,
        "replicas": replicas,
        "source": "deadman-safe-mcp",
    }


@mcp.tool(name="statuspage.post")
def statuspage_post(message: str, severity: str = "info") -> dict[str, Any]:
    """Simulate posting an incident update."""
    update = {
        "message": message,
        "severity": severity,
        "created_at": int(time.time()),
        "source": "deadman-safe-mcp",
    }
    _status_updates.append(update)
    return {"posted": True, "update": update, "count": len(_status_updates)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run DEADMAN safe demo MCP server.")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp")
    args = parser.parse_args()

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port, path=args.path)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
