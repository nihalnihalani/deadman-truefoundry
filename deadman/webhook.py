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
import hashlib
import hmac
import json
import logging
import os
import uuid
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import deadman.config as config
from deadman import state as _state_module
from deadman.world import World, RealWorld
from deadman.chaos import Chaos
from deadman.commander import NaiveAgent, Deadman, REVERT_KEY, action_key
from deadman.mcp_gateway import KillSignal
from deadman.state import AuditLog, DurableState

logger = logging.getLogger("deadman.webhook")

# ---------------------------------------------------------------------------
# App + CORS
# ---------------------------------------------------------------------------

app = FastAPI(title="DEADMAN — resilient incident commander")

# CORS: localhost dev origins only. We authenticate with a bearer/HMAC secret,
# not cookies, so credentials are NOT allowed (avoids the credentialed-wildcard
# foot-gun) and methods/headers are explicit rather than wildcards.
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
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Deadman-Signature"],
)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

async def _verify_webhook_auth(request: Request) -> None:
    """Verify the incident webhook is authenticated when DEADMAN_WEBHOOK_SECRET is set.

    No secret configured -> open (mock/dev default; keeps demo + tests working).
    Secret configured -> require ONE of:
      * Authorization: Bearer <secret>
      * X-Deadman-Signature: <hex HMAC-SHA256(raw_body, secret)>  (PagerDuty/CloudWatch sign payloads)
    Raises HTTPException(401) on mismatch.
    """
    secret = config.webhook_secret()
    if not secret:
        if config.is_real():
            raise HTTPException(
                status_code=503,
                detail="DEADMAN_WEBHOOK_SECRET is required in real mode",
            )
        return

    # 1) Bearer token (constant-time compare).
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
        if hmac.compare_digest(token, secret):
            return

    # 2) HMAC signature over the raw body.
    sig = request.headers.get("x-deadman-signature", "")
    if sig:
        raw = await request.body()
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        # tolerate an optional "sha256=" prefix some signers use
        provided = sig.split("=", 1)[1] if sig.startswith("sha256=") else sig
        if hmac.compare_digest(provided, expected):
            return

    raise HTTPException(status_code=401, detail="invalid or missing webhook authentication")


def _require_demo_enabled() -> None:
    """Block demo/chaos endpoints when DEADMAN_ENABLE_DEMO=0 (production)."""
    if not config.demo_enabled():
        raise HTTPException(status_code=404, detail="demo endpoints are disabled")


def _validate_http_incident_id(incident_id: str) -> str:
    try:
        return config.validate_incident_id(incident_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

# ---------------------------------------------------------------------------
# OTel init (no-op when OTEL not configured)
# ---------------------------------------------------------------------------
from deadman.otel import init_otel  # noqa: E402

init_otel(app)

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

def _scoreboard_dict(sb, incident_id: str | None = None, summary: str | None = None) -> dict:
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
    if summary is not None:
        d["summary"] = summary
    return d


def _run_incident(incident_id: str) -> "Scoreboard":  # type: ignore[name-defined]
    """Blocking agent run, offloaded to a worker thread by the handler.

    Real mode wires the production `RealWorld` system-of-record adapter (sharing the
    incident's durable AuditLog), so the resume path reconciles against the LIVE provider
    instead of an in-memory mock — this is what makes "exactly-once across process death"
    true in the running server, not just in tests. Mock mode keeps the in-memory `World`.

    The handler is resume-aware: if durable state already holds a pending (uncommitted)
    action for this incident — i.e. a previous process crashed mid-mitigation — we resume
    (rehydrate + dedupe) rather than start a fresh run, so a re-delivered alert can never
    double-execute the in-flight destructive action.
    """
    incident_id = config.validate_incident_id(incident_id)
    if config.is_real():
        world = RealWorld(audit_log=AuditLog(incident_id))
    else:
        world = World()
    resume = DurableState(incident_id).pending is not None
    return Deadman(incident_id, world, chaos=None).run(resume=resume)


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    return {"ok": True, "mode": config.MODE}


@app.get("/readyz")
def readyz():
    status = config.readiness()
    return JSONResponse(status_code=200 if status["ok"] else 503, content=status)


# ---------------------------------------------------------------------------
# /incident  (existing production webhook)
# ---------------------------------------------------------------------------


@app.post("/incident")
async def handle_incident(inc: Incident, request: Request):
    incident_id = _validate_http_incident_id(inc.incident_id)

    # Optional shared-secret auth (no-op when DEADMAN_WEBHOOK_SECRET is unset).
    await _verify_webhook_auth(request)

    # Structured log of the incoming alert summary so the alert text is recorded.
    # NOTE: the summary is echoed back and logged here; full prompt-wiring (feeding
    # the alert text into the commander's diagnosis prompt) is a follow-up — it
    # requires a Deadman.run() signature change owned by Anchor, so we do NOT claim
    # it currently drives diagnosis.
    logger.info(
        "incident received", extra={"incident_id": incident_id, "summary": inc.summary}
    )

    # The agent's model/tool calls block (real-mode retries sleep up to a few
    # seconds); offload to a worker thread so the event loop stays responsive
    # during a 5xx storm. Mock-mode behaviour is identical.
    sb = await run_in_threadpool(_run_incident, incident_id)
    return _scoreboard_dict(sb, incident_id, summary=inc.summary)


# ---------------------------------------------------------------------------
# /incident/{id}/postmortem
# ---------------------------------------------------------------------------


@app.get("/incident/{incident_id}/postmortem")
def postmortem(incident_id: str):
    incident_id = _validate_http_incident_id(incident_id)
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
    revert_key = action_key(incident_id, "revert_pr", "PR-1337")
    _state_module.reset(incident_id)
    world = World()
    chaos = Chaos()
    chaos.correlated_blackout()
    chaos.rate_limit_storm()
    chaos.corrupt_output = True
    chaos.kill_process_after(revert_key)

    agent = Deadman(incident_id, world, chaos)
    try:
        agent.run()
    except KillSignal:
        pass
    chaos.kill_after = None
    return Deadman(incident_id, world, chaos).run(resume=True)


def _run_demo_both():
    """Run both demo agents (blocking); offloaded to a worker thread."""
    return _run_naive_demo(), _run_deadman_demo()


@app.post("/api/demo/run")
async def demo_run():
    _require_demo_enabled()
    naive, dead = await run_in_threadpool(_run_demo_both)
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

    # Run the real agents (blocking) on a worker thread to get final scoreboards
    # without blocking the event loop while the SSE stream is paced out.
    naive_final = await run_in_threadpool(_run_naive_demo)
    dead_final = await run_in_threadpool(_run_deadman_demo)

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
    _require_demo_enabled()
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
    _require_demo_enabled()
    if toggle not in _VALID_TOGGLES:
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
_web_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
if os.path.isdir(_web_dir):
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="ui")
