"""Cloud webhook — receives an incident alert and runs the DEADMAN commander.

    uvicorn deadman.webhook:app --reload --port 8080

POST /incident  { "incident_id": "inc-123", "summary": "p1: payments-db latency spike" }
GET  /incident/{id}/postmortem   -> the audit-log-derived postmortem

This is the entrypoint a PagerDuty / CloudWatch alarm hits. In real mode the commander's
model calls go through deadman.realmode.complete (TFY AI Gateway) and tool calls through
deadman.realmode.call_tool (TFY MCP Gateway).
"""
from __future__ import annotations
from fastapi import FastAPI
from pydantic import BaseModel

from deadman.world import World
from deadman.chaos import Chaos
from deadman.commander import Deadman
from deadman.state import AuditLog

app = FastAPI(title="DEADMAN — resilient incident commander")


class Incident(BaseModel):
    incident_id: str
    summary: str = ""


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/incident")
def handle_incident(inc: Incident):
    # A real deployment injects no chaos; resilience comes from the gateways.
    agent = Deadman(inc.incident_id, World(), Chaos())
    sb = agent.run()
    return {
        "incident_id": inc.incident_id,
        "survived": sb.survived,
        "backend": sb.backend,
        "fallback_depth": sb.fallback_depth,
        "double_executions": sb.double_executions,
        "guardrail_blocks": sb.guardrail_blocks,
        "drain_authority": sb.drain_authority,
        "timeline": sb.notes,
    }


@app.get("/incident/{incident_id}/postmortem")
def postmortem(incident_id: str):
    return {"incident_id": incident_id, "audit_trail": AuditLog(incident_id).postmortem()}
