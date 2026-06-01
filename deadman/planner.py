"""DEADMAN planner — parse the model's text output into a structured Action.

The agentic loop uses a TEXT-based ReAct (Reason+Act) pattern over the existing
AIGateway.complete(prompt) -> Completion(text, ...) interface.  The LLM is
instructed to emit a single JSON object per turn; this module extracts and
validates it.

Public API
----------
  parse_action(text: str) -> Action
      Extract the first JSON object from `text`.  Tolerates Markdown code-fences
      and surrounding prose.  On any parse failure → returns a safe "hold" Action
      so the loop never acts blindly on bad output.

  build_prompt(summary, observations, catalog) -> str
      Assemble the reasoning prompt sent to the LLM.

Action contract (JSON object emitted by the model)
---------------------------------------------------
  Mitigate a tool:
    {"tool": "github.revert_pr", "args": {"pr": "PR-42"}, "rationale": "...", "done": false}

  Declare incident resolved:
    {"done": true, "rationale": "incident resolved — all clear"}

  Safe-hold / no-op:
    {} or malformed → planner returns Action(tool=None, done=False, rationale="...")
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Action:
    tool: str | None        # tool name from REGISTRY, or None for safe-hold
    args: dict              # args dict to pass to MCPGateway.execute()
    rationale: str          # model's reasoning text (for audit / postmortem)
    done: bool              # True → model declares incident resolved


_SAFE_HOLD = Action(
    tool=None,
    args={},
    rationale="unparseable model output → safe-hold (no action taken)",
    done=False,
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# Match the first balanced JSON object in arbitrary text.  The regex captures
# the whole {...} block; we then let json.loads() do the real validation.
_JSON_OBJ_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}", re.DOTALL)

# Markdown code fence (```json ... ``` or ``` ... ```)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences, leaving just the inner content."""
    match = _CODE_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text


def _extract_json(text: str) -> dict | None:
    """Find and parse the first JSON object in text.  Returns None on failure."""
    text = _strip_fences(text)

    # First try: the whole (stripped) text as JSON
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Second try: find the first {...} block (handles surrounding prose)
    # Use a more robust approach: find matching braces
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except (json.JSONDecodeError, ValueError):
                    # Keep scanning; maybe the first { was inside prose
                    next_start = text.find("{", start + 1)
                    if next_start == -1:
                        return None
                    start = next_start
                    depth = 0
                break
    return None


def parse_action(text: str) -> "Action":
    """Parse the model's text response into a structured Action.

    Handles:
    - JSON in a Markdown code fence
    - JSON preceded/followed by prose rationale
    - Fully malformed output → returns the safe-hold sentinel (no crash, no action)
    """
    if not text or not text.strip():
        return Action(
            tool=None,
            args={},
            rationale="empty model output → safe-hold",
            done=False,
        )

    obj = _extract_json(text)
    if obj is None:
        return Action(
            tool=None,
            args={},
            rationale=f"unparseable model output → safe-hold: {text[:120]!r}",
            done=False,
        )

    # ── done declaration ──────────────────────────────────────────────────────
    done = bool(obj.get("done", False))
    rationale = str(obj.get("rationale", ""))

    if done:
        return Action(tool=None, args={}, rationale=rationale, done=True)

    # ── tool call ─────────────────────────────────────────────────────────────
    tool = obj.get("tool")
    args = obj.get("args", {})

    if not tool or not isinstance(tool, str):
        return Action(
            tool=None,
            args={},
            rationale=rationale or "model omitted 'tool' field → safe-hold",
            done=False,
        )
    if not isinstance(args, dict):
        return Action(
            tool=None,
            args={},
            rationale=f"model provided non-dict args for {tool!r} → safe-hold",
            done=False,
        )

    return Action(tool=tool.strip(), args=args, rationale=rationale, done=False)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_prompt(summary: str, observations: list[str], catalog: str) -> str:
    """Assemble the reasoning prompt sent to the LLM.

    Parameters
    ----------
    summary     : Short incident description (from the webhook payload).
    observations: List of human-readable strings from prior loop steps (tool results,
                  blocked actions, safe-holds).
    catalog     : Output of tools.tool_catalog_prompt() — the agent's tool menu.
    """
    parts: list[str] = [
        "You are DEADMAN, an incident-commander AI.",
        "Your job: diagnose and mitigate the active incident ONE STEP AT A TIME.",
        "",
        f"INCIDENT SUMMARY:\n{summary}",
        "",
        catalog,
        "",
    ]

    if observations:
        parts.append("OBSERVATIONS SO FAR (from previous steps):")
        for i, obs in enumerate(observations, 1):
            parts.append(f"  {i}. {obs}")
        parts.append("")

    parts += [
        "INSTRUCTIONS:",
        "  - Respond with EXACTLY ONE JSON action object, nothing else.",
        "  - Choose the most appropriate next action based on observations so far.",
        "  - If the incident is fully resolved, set done=true.",
        "  - If unsure, run a read-only diagnostic tool first.",
        "  - Never repeat a tool call that already succeeded (check observations).",
        "",
        "Your JSON action:",
    ]

    return "\n".join(parts)
