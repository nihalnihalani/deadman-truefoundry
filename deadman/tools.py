"""DEADMAN tool registry — single source of truth for what the agent can do.

Each Tool entry describes:
  name          : dot-namespaced identifier matching MCPGateway.execute() tool names.
  scope         : the scope token MCPGateway / AgentGateway use for Cedar enforcement.
  destructive   : whether the tool mutates production state.
  args_schema   : dict describing the required args (simple JSONSchema-like spec).
  target_field  : the arg name that uniquely identifies the idempotency target.
  description   : one-line summary for the LLM prompt.

This module has NO I/O — it is pure config/data.  MCPGateway maps tool names to side
effects; this registry is the agent's menu + schema + prompt-builder.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tool:
    name: str
    scope: str
    destructive: bool
    args_schema: dict
    target_field: str
    description: str


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

REGISTRY: dict[str, Tool] = {
    t.name: t for t in [
        # ── Read-only / diagnostic ────────────────────────────────────────────
        Tool(
            name="cw.get_metrics",
            scope="cw.get_metrics",
            destructive=False,
            args_schema={"required": [], "optional": ["namespace", "metric"]},
            target_field="metric",
            description="Fetch CloudWatch metrics for the incident (read-only).",
        ),
        Tool(
            name="logs.query",
            scope="logs.query",
            destructive=False,
            args_schema={"required": [], "optional": ["log_group", "query"]},
            target_field="log_group",
            description="Query application logs for error patterns (read-only).",
        ),
        Tool(
            name="k8s.describe",
            scope="k8s.describe",
            destructive=False,
            args_schema={"required": ["node"], "optional": []},
            target_field="node",
            description="Describe a Kubernetes node to check health/status (read-only).",
        ),
        # ── Destructive — require destructive scope ───────────────────────────
        Tool(
            name="github.revert_pr",
            scope="github.revert_pr",
            destructive=True,
            args_schema={"required": ["pr"], "optional": []},
            target_field="pr",
            description=(
                "Revert a GitHub pull request that caused the incident. "
                "DESTRUCTIVE — requires full scope."
            ),
        ),
        Tool(
            name="k8s.cordon_drain",
            scope="k8s.cordon_drain",
            destructive=True,
            args_schema={"required": ["node"], "optional": ["namespace", "elevation"]},
            target_field="node",
            description=(
                "Cordon and drain a Kubernetes node. "
                "DESTRUCTIVE — requires full scope."
            ),
        ),
        Tool(
            name="asg.scale",
            scope="asg.scale",
            destructive=True,
            args_schema={"required": ["asg", "replicas"], "optional": []},
            target_field="asg",
            description=(
                "Scale an Auto Scaling Group to `replicas` instances. "
                "DESTRUCTIVE — requires full scope; min replicas enforced by guardrail."
            ),
        ),
        # ── Notification ─────────────────────────────────────────────────────
        Tool(
            name="statuspage.post",
            scope="statuspage.post",
            destructive=False,
            args_schema={"required": ["message"], "optional": ["severity"]},
            target_field="message",
            description="Post an update to the public status page (non-destructive).",
        ),
    ]
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tool_catalog_prompt() -> str:
    """Return a compact text description of all registered tools for the LLM prompt."""
    lines = ["Available tools (respond with ONE JSON action per turn):"]
    for tool in REGISTRY.values():
        destructive_tag = " [DESTRUCTIVE]" if tool.destructive else ""
        req_args = tool.args_schema.get("required", [])
        opt_args = tool.args_schema.get("optional", [])
        args_desc = ""
        if req_args:
            args_desc += "required: " + ", ".join(req_args)
        if opt_args:
            if args_desc:
                args_desc += "; "
            args_desc += "optional: " + ", ".join(opt_args)
        lines.append(f"  {tool.name}{destructive_tag}: {tool.description}")
        if args_desc:
            lines.append(f"    args: {{{args_desc}}}")
    lines.append("")
    lines.append(
        'JSON action format: {"tool": "<name>", "args": {...}, "rationale": "...", "done": false}'
    )
    lines.append(
        'When the incident is resolved: {"done": true, "rationale": "..."}'
    )
    return "\n".join(lines)


def idempotency_target(tool: "Tool", args: dict) -> str:
    """Return the idempotency target string for a given tool call.

    Falls back to "default" when the target_field is absent from args (e.g. for
    read-only tools like cw.get_metrics where the target field is optional).
    """
    return str(args.get(tool.target_field, "default"))


def validate_args(tool: "Tool", args: dict) -> bool:
    """Return True when all required args are present; raises ValueError otherwise."""
    missing = [k for k in tool.args_schema.get("required", []) if k not in args]
    if missing:
        raise ValueError(
            f"Tool '{tool.name}' missing required args: {missing}. Got: {list(args)}"
        )
    return True
