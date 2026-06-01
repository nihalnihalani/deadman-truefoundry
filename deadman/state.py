"""Durable state + audit log — the crown jewel.

Both are file-backed so they SURVIVE process death (the mock stand-in for DynamoDB +
the TrueFoundry MCP Gateway's OpenTelemetry audit log). The audit log is load-bearing
THREE ways: (1) recovery ledger, (2) exactly-once dedup on resume, (3) the postmortem.
"""
from __future__ import annotations
import json
import os
import deadman.config as config


def _path(incident_id: str, name: str) -> str:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    return os.path.join(config.STATE_DIR, f"{incident_id}.{name}")


class DurableState:
    """Append-only incident state machine, OUTSIDE the model provider."""

    def __init__(self, incident_id: str):
        self.incident_id = incident_id
        self.path = _path(incident_id, "state.json")
        self.data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path) as f:
                return json.load(f)
        return {"incident_id": self.incident_id, "phase": "triage",
                "actions_committed": [], "pending_action": None, "timeline": []}

    def _flush(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def set_pending(self, action: str, key: str):
        self.data["pending_action"] = {"action": action, "key": key}
        self._flush()

    def commit(self, action: str, key: str):
        self.data["actions_committed"].append({"action": action, "key": key})
        self.data["pending_action"] = None
        self._flush()

    def note(self, msg: str):
        self.data["timeline"].append(msg)
        self._flush()

    @property
    def pending(self):
        return self.data.get("pending_action")


class AuditLog:
    """Append-only JSONL — the MCP Gateway audit trail. is_committed() enforces exactly-once."""

    def __init__(self, incident_id: str):
        self.path = _path(incident_id, "audit.jsonl")

    def write(self, entry: dict):
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _entries(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def is_committed(self, key: str) -> bool:
        return any(e.get("key") == key and e.get("status") == "COMMITTED" for e in self._entries())

    def pending_keys(self) -> list[str]:
        committed = {e["key"] for e in self._entries() if e.get("status") == "COMMITTED"}
        return [e["key"] for e in self._entries()
                if e.get("status") == "PENDING" and e["key"] not in committed]

    def postmortem(self) -> list[str]:
        return [f"{e['status']:9} {e.get('tool','')} key={e.get('key','')}" for e in self._entries()]


def reset(incident_id: str):
    for name in ("state.json", "audit.jsonl"):
        p = _path(incident_id, name)
        if os.path.exists(p):
            os.remove(p)
