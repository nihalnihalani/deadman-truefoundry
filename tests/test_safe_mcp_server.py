from __future__ import annotations

import asyncio

from mcp_servers.deadman_safe_tools import mcp


def test_safe_mcp_server_exposes_deadman_tool_names():
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}

    assert {
        "cw.get_metrics",
        "logs.query",
        "k8s.describe",
        "github.get_pr_state",
        "github.revert_pr",
        "k8s.cordon_drain",
        "asg.scale",
        "statuspage.post",
    } <= names


def test_safe_mcp_server_simulates_revert_and_readback():
    asyncio.run(mcp.call_tool("github.revert_pr", {"pr": "PR-test"}))
    state = asyncio.run(mcp.call_tool("github.get_pr_state", {"pr": "PR-test"}))

    assert state.structured_content["reverted"] is True
    assert state.structured_content["state"] == "reverted"
