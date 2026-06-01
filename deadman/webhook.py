"""DEADMAN webhook — incident commander HTTP entrypoint.

Endpoints
---------
GET  /healthz
POST /incident
GET  /incident/{id}/postmortem
POST /api/demo/run
GET  /api/demo/stream          (Server-Sent Events)
POST /api/chaos/{toggle}
GET  /                         (serves web/ static UI if present)

Run:
    uvicorn deadman.webhook:app --reload --port 8080
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import deadman.config as config
from deadman import state as _state_module
from deadman.world import World
from deadman.chaos import Chaos
from deadman.commander import NaiveAgent, Deadman, REVERT_KEY
from deadman.mcp_gateway import KillSignal
from deadman.state import AuditLog

# ---------------------------------------------------------------------------
# App + CORS
# ---------------------------------------------------------------------------

app = FastAPI(title="DEADMAN — resilient incident commander")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# OTel init (no-op when OTEL not configured)
# ---------------------------------------------------------------------------
from deadman.otel import init_otel  # noqa: E402

init_otel(app)

import os as _os  # noqa: E402

# ---------------------------------------------------------------------------
# Server-held demo Chaos state (used by /api/chaos/{toggle})
# ---------------------------------------------------------------------------
_demo_chaos = Chaos()

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Incident(BaseModel):
    incident_id: str
    summary: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scoreboard_dict(sb, incident_id: str | None = None) -> dict:
    """Serialise a Scoreboard to the canonical JSON shape."""
    d: dict = {
        "survived": sb.survived,
        "backend": sb.backend,
        "fallback_depth": sb.fallback_depth,
        "double_executions": sb.double_executions,
        "guardrail_blocks": sb.guardrail_blocks,
        "drain_authority": sb.drain_authority,
        "timeline": sb.notes,
        "mode": config.MODE,
    }
    if incident_id:
        d["incident_id"] = incident_id
    return d


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    return {"ok": True, "mode": config.MODE}


# ---------------------------------------------------------------------------
# /incident  (existing production webhook)
# ---------------------------------------------------------------------------


@app.post("/incident")
def handle_incident(inc: Incident):
    agent = Deadman(inc.incident_id, World(), chaos=None)
    sb = agent.run()
    return _scoreboard_dict(sb, inc.incident_id)


# ---------------------------------------------------------------------------
# /incident/{id}/postmortem
# ---------------------------------------------------------------------------


@app.get("/incident/{incident_id}/postmortem")
def postmortem(incident_id: str):
    return {"incident_id": incident_id, "audit_trail": AuditLog(incident_id).postmortem()}


# ---------------------------------------------------------------------------
# /api/demo/run  — runs BOTH agents deterministically and returns comparison
# ---------------------------------------------------------------------------

_DEMO_INCIDENT = "demo-incident-api"


def _run_naive_demo() -> "Scoreboard":  # type: ignore[name-defined]
    chaos = Chaos()
    chaos.correlated_blackout()
    chaos.kill_bedrock()
    return NaiveAgent(World()).run(chaos)


def _run_deadman_demo() -> "Scoreboard":  # type: ignore[name-defined]
    incident_id = _DEMO_INCIDENT + "-" + uuid.uuid4().hex[:8]
    _state_module.reset(incident_id)
    world = World()
    chaos = Chaos()
    chaos.correlated_blackout()
    chaos.rate_limit_storm()
    chaos.corrupt_output = True
    chaos.kill_process_after(REVERT_KEY)

    agent = Deadman(incident_id, world, chaos)
    try:
        agent.run()
    except KillSignal:
        pass
    chaos.kill_after = None
    return Deadman(incident_id, world, chaos).run(resume=True)


@app.post("/api/demo/run")
def demo_run():
    naive = _run_naive_demo()
    dead = _run_deadman_demo()
    return {
        "naive": _scoreboard_dict(naive),
        "deadman": _scoreboard_dict(dead),
        "headline": {
            "double_executions_naive": naive.double_executions,
            "double_executions_deadman": dead.double_executions,
        },
    }


# ---------------------------------------------------------------------------
# /api/demo/stream  — SSE beat-by-beat replay
# ---------------------------------------------------------------------------

# The demo beats table (mirrors README 4-minute demo).
# Each beat: (label, side, beat_name, note_text, chaos_toggle_fn | None)
_BEATS = [
    # t, beat, side, note, chaos_setup
    ("0:00", "cold_open", "both",
     "Agents initialized — naive (raw Bedrock) vs DEADMAN (TFY gateway).", None),
    ("0:20", "correlated_blackout", "both",
     "us-east-1 EVENT: region + provider + tools degrade simultaneously.", "correlated_blackout"),
    ("0:45", "rate_limit_storm", "deadman",
     "429 storm on tier-0 — AI Gateway sheds to tier-1 (us-west-2).", "rate_limit_storm"),
    ("1:10", "all_bedrock_down", "both",
     "All Bedrock tiers down + tier-1 failure injected → auto-leash fires.", "kill_bedrock"),
    ("1:40", "corrupt_output", "deadman",
     "Degraded API returns corrupt JSON — Post-Tool guardrail catches it.", "corrupt_output"),
    ("2:10", "kill_mid_rollback", "deadman",
     "SIGKILL between side-effect and COMMIT — DEADMAN survives via durable state.", "kill_mid_rollback"),
    ("2:50", "recovery", "deadman",
     "Fresh process resumes; audit log shows COMMITTED → skips re-execution (0 double-executions).", None),
    ("3:20", "naive_double_exec", "naive",
     "Naive agent restarts from scratch → re-fires revert_pr → DOUBLE-EXECUTION.", None),
    ("3:50", "done", "both",
     "Demo complete. NAIVE double_executions=1, DEADMAN double_executions=0.", None),
]


async def _sse_demo_generator(fast: bool) -> AsyncGenerator[str, None]:
    """Yield SSE events for the split-screen demo replay."""
    delay = 0.0 if fast else 0.4

    incident_id = "sse-demo-" + uuid.uuid4().hex[:8]
    _state_module.reset(incident_id)

    # Build up scoreboard state incrementally.
    naive_sb: dict = {
        "survived": False,
        "backend": "us-east-1 (raw)",
        "fallback_depth": 0,
        "double_executions": 0,
        "guardrail_blocks": 0,
        "drain_authority": "ON",
        "timeline": [],
        "mode": config.MODE,
    }
    deadman_sb: dict = {
        "survived": False,
        "backend": "tier-0",
        "fallback_depth": 0,
        "double_executions": 0,
        "guardrail_blocks": 0,
        "drain_authority": "ON",
        "timeline": [],
        "mode": config.MODE,
    }

    # Run the real agents in the background to get final scoreboards.
    naive_final = _run_naive_demo()
    dead_final = _run_deadman_demo()

    def _emit(beat_dict: dict) -> str:
        return "data: " + json.dumps(beat_dict) + "\n\n"

    for t, beat, side, note, chaos_key in _BEATS:
        if beat == "done":
            # Use real final scoreboards for the done event.
            final_naive = _scoreboard_dict(naive_final)
            final_deadman = _scoreboard_dict(dead_final)
            payload = {
                "t": t,
                "beat": "done",
                "side": "both",
                "scoreboard": {
                    "naive": final_naive,
                    "deadman": final_deadman,
                },
                "note": note,
                "headline": {
                    "double_executions_naive": naive_final.double_executions,
                    "double_executions_deadman": dead_final.double_executions,
                },
            }
            yield _emit(payload)
            if delay > 0:
                await asyncio.sleep(delay)
            return

        # Gradually update the appropriate scoreboard for visual pacing.
        if side in ("deadman", "both") and beat not in ("naive_double_exec",):
            deadman_sb["timeline"] = list(deadman_sb.get("timeline", [])) + [note]
        if side in ("naive", "both") and beat not in ("cold_open",):
            naive_sb["timeline"] = list(naive_sb.get("timeline", [])) + [note]

        # Reveal incremental state changes per beat.
        if beat == "correlated_blackout":
            naive_sb["backend"] = "us-east-1 (degraded)"
            deadman_sb["backend"] = "tier-0 (degraded)"
        elif beat == "rate_limit_storm":
            deadman_sb["fallback_depth"] = 1
            deadman_sb["backend"] = "tier-1 (us-west-2)"
        elif beat == "all_bedrock_down":
            deadman_sb["fallback_depth"] = max(deadman_sb["fallback_depth"], 2)
            deadman_sb["drain_authority"] = "OFF"
            deadman_sb["backend"] = "tier-2 (llama4)"
        elif beat == "corrupt_output":
            deadman_sb["guardrail_blocks"] = 1
        elif beat == "kill_mid_rollback":
            deadman_sb["timeline"] = list(deadman_sb.get("timeline", [])) + [
                "KILLED mid-rollback — durable state preserved"
            ]
        elif beat == "recovery":
            deadman_sb["survived"] = True
            deadman_sb["double_executions"] = 0
        elif beat == "naive_double_exec":
            naive_sb["double_executions"] = 1
            naive_sb["survived"] = False

        payload = {
            "t": t,
            "beat": beat,
            "side": side,
            "scoreboard": {
                "naive": dict(naive_sb),
                "deadman": dict(deadman_sb),
            },
            "note": note,
        }
        yield _emit(payload)
        if delay > 0:
            await asyncio.sleep(delay)


@app.get("/api/demo/stream")
async def demo_stream(fast: int = 0):
    """Server-Sent Events replay of the demo, beat by beat."""
    return StreamingResponse(
        _sse_demo_generator(fast=bool(fast)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# /api/chaos/{toggle}  — UI chaos buttons
# ---------------------------------------------------------------------------

_VALID_TOGGLES = frozenset(
    {"correlated_blackout", "rate_limit_storm", "kill_bedrock", "corrupt_output",
     "kill_mid_rollback", "reset"}
)


def _chaos_state_dict(c: Chaos) -> dict:
    return {
        "correlated_blackout": bool(c.down_regions),
        "rate_limit_storm": bool(c.slow_tiers),
        "kill_bedrock": c.all_bedrock_down,
        "corrupt_output": c.corrupt_output,
        "kill_mid_rollback": c.kill_after is not None,
        "down_regions": sorted(c.down_regions),
        "down_tiers": sorted(c.down_tiers),
    }


@app.post("/api/chaos/{toggle}")
def chaos_toggle(toggle: str):
    global _demo_chaos
    if toggle not in _VALID_TOGGLES:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"Unknown toggle '{toggle}'. Valid: {sorted(_VALID_TOGGLES)}",
        )
    if toggle == "reset":
        _demo_chaos = Chaos()
    elif toggle == "correlated_blackout":
        _demo_chaos.correlated_blackout()
    elif toggle == "rate_limit_storm":
        _demo_chaos.rate_limit_storm()
    elif toggle == "kill_bedrock":
        _demo_chaos.kill_bedrock()
    elif toggle == "corrupt_output":
        _demo_chaos.corrupt_output = not _demo_chaos.corrupt_output
    elif toggle == "kill_mid_rollback":
        # Arm the SIGKILL between the revert side-effect and its COMMIT — the headline beat.
        _demo_chaos.kill_process_after(REVERT_KEY)

    return {"toggle": toggle, "chaos": _chaos_state_dict(_demo_chaos)}


# ---------------------------------------------------------------------------
# Static UI mount (web/ served at /) — MUST be last so API routes win.
# Guarded: won't crash if web/ doesn't exist yet.
# ---------------------------------------------------------------------------
_web_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "web")
if _os.path.isdir(_web_dir):
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="ui")
