"""Durable state + audit log — the crown jewel.

Pluggable backend selected by config.STATE_BACKEND:

  "file"     (default) — byte-compatible with original behavior. Files live at
             {STATE_DIR}/{incident_id}.state.json and {incident_id}.audit.jsonl.

  "dynamodb" — production-grade, exactly-once at the storage layer via DynamoDB
             conditional writes.

  DynamoDB table schema
  ---------------------
  Table name: config.DYNAMODB_TABLE  (default: "deadman-incident-state")

  Partition key  PK  (S)  — incident_id, e.g. "incident-42"
  Sort key       SK  (S)  — record type + sub-key:
      "STATE"              — the single mutable state item for the incident
      "AUDIT#<seq>"        — monotonically increasing audit records
                             seq is zero-padded 12-digit int, e.g. "AUDIT#000000000001"

  State item attributes:
      PK, SK="STATE", data (JSON-encoded state blob), version (N) for optimistic locking

  Audit item attributes:
      PK, SK="AUDIT#<seq>", status (S), tool (S), key (S), plus any extra fields from entry.
      A CONDITIONAL write on the SK prevents duplicate COMMITTED records for the same
      idempotency key: the condition `attribute_not_exists(PK)` ensures each SK is written
      at most once. For the COMMITTED record specifically a separate GSI or scan checks
      whether a COMMITTED record for `key` already exists before calling is_committed().

  GSI (optional, for is_committed hot path):
      GSI name: "KeyStatusIndex"
      GSI PK: key (S), GSI SK: status (S) — allows is_committed(key) via a direct query
      rather than a full partition scan.

Both the file and DynamoDB paths satisfy the same public interface; the rest of the
codebase never touches backend internals.
"""
from __future__ import annotations
import json
import os
import deadman.config as config


# ---------------------------------------------------------------------------
# helpers shared by both backends
# ---------------------------------------------------------------------------

def _file_path(incident_id: str, name: str) -> str:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    return os.path.join(config.STATE_DIR, f"{incident_id}.{name}")


def _empty_state(incident_id: str) -> dict:
    return {
        "incident_id": incident_id,
        "phase": "triage",
        "actions_committed": [],
        "pending_action": None,
        "timeline": [],
    }


# ---------------------------------------------------------------------------
# File backend (default — mock/demo/CI)
# ---------------------------------------------------------------------------

class FileBackend:
    """Byte-compatible with the original state.py implementation."""

    def __init__(self, incident_id: str):
        self.incident_id = incident_id
        self._state_path = _file_path(incident_id, "state.json")
        self._audit_path = _file_path(incident_id, "audit.jsonl")
        self._lock_path = _file_path(incident_id, "lock")

    # ---- state ----

    def load_state(self) -> dict:
        if os.path.exists(self._state_path):
            with open(self._state_path) as f:
                return json.load(f)
        return _empty_state(self.incident_id)

    def save_state(self, data: dict):
        with open(self._state_path, "w") as f:
            json.dump(data, f, indent=2)

    # ---- audit ----

    def append_audit(self, entry: dict):
        with open(self._audit_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_audit(self) -> list[dict]:
        if not os.path.exists(self._audit_path):
            return []
        with open(self._audit_path) as f:
            return [json.loads(line) for line in f if line.strip()]

    # ---- is_committed: conditional write semantics (file version: check before write) ----

    def is_committed(self, key: str) -> bool:
        return any(
            e.get("key") == key and e.get("status") == "COMMITTED"
            for e in self.read_audit()
        )

    # ---- atomic claim (exactly-once commit) ----

    def claim_commit(self, key: str, tool: str = "") -> bool:
        """Atomically write a COMMITTED record for `key` IFF none exists yet.

        Race-safety: the entire (read_audit -> check committed -> append COMMITTED)
        critical section runs under an exclusive fcntl.flock(LOCK_EX) on a per-incident
        lock file ({STATE_DIR}/{incident_id}.lock). This closes the check-then-act TOCTOU
        window for concurrent processes on the same host. The lock is always released in
        the finally block.

        Returns True if THIS call wrote the COMMITTED record (won the claim), False if a
        COMMITTED record for `key` already existed (replay / losing process).
        """
        import fcntl  # POSIX advisory lock; lazy import keeps non-claim paths dependency-free
        lock_f = open(self._lock_path, "w")
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            if self.is_committed(key):
                return False  # someone already committed this key
            self.append_audit({"status": "COMMITTED", "tool": tool, "key": key})
            return True
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            lock_f.close()

    # ---- reset ----

    def reset(self):
        for p in (self._state_path, self._audit_path, self._lock_path):
            if os.path.exists(p):
                os.remove(p)


# ---------------------------------------------------------------------------
# DynamoDB backend (production)
# ---------------------------------------------------------------------------

class DynamoDBBackend:
    """Production DynamoDB backend.

    boto3 is imported lazily so it is never required when running in file mode.

    Table schema (reproduced from module docstring):
      PK (S): incident_id
      SK (S): "STATE" | "AUDIT#<seq>"
    Audit items carry status/tool/key attributes used by is_committed().
    Conditional writes on AUDIT items (`attribute_not_exists(PK)`) guarantee
    each sort-key is written exactly once, making COMMITTED records idempotent
    at the storage layer.
    """

    _SEQ_PAD = 12  # zero-padded sequence width

    def __init__(self, incident_id: str):
        # lazy import — never loaded when STATE_BACKEND == "file"
        import boto3  # type: ignore
        from botocore.exceptions import ClientError  # type: ignore  # noqa: F401
        self._ClientError = ClientError

        self.incident_id = incident_id
        self._table_name = config.DYNAMODB_TABLE
        self._dynamodb = boto3.resource("dynamodb", region_name=config.AWS_REGION)
        self._table = self._dynamodb.Table(self._table_name)

    def _state_key(self) -> dict:
        return {"PK": self.incident_id, "SK": "STATE"}

    def _audit_sk(self, seq: int) -> str:
        return f"AUDIT#{seq:0{self._SEQ_PAD}d}"

    def _next_seq(self) -> int:
        """Count existing AUDIT items to derive the next monotonic sequence number."""
        resp = self._table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :pfx)",
            ExpressionAttributeValues={":pk": self.incident_id, ":pfx": "AUDIT#"},
            Select="COUNT",
        )
        return resp.get("Count", 0)

    # ---- state ----

    def load_state(self) -> dict:
        resp = self._table.get_item(Key=self._state_key())
        item = resp.get("Item")
        if item:
            return json.loads(item["data"])
        return _empty_state(self.incident_id)

    def save_state(self, data: dict):
        self._table.put_item(Item={
            **self._state_key(),
            "data": json.dumps(data),
        })

    # ---- audit ----

    def append_audit(self, entry: dict):
        seq = self._next_seq()
        sk = self._audit_sk(seq)
        item = {
            "PK": self.incident_id,
            "SK": sk,
            **{k: str(v) if not isinstance(v, str) else v for k, v in entry.items()},
        }
        # Conditional write: attribute_not_exists(PK) ensures each SK is written at most once.
        # If a duplicate COMMITTED record arrives for the same SK, the condition fails and
        # we swallow the ConditionalCheckFailedException — the record is already there.
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except self._ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                pass  # idempotent: already written
            else:
                raise

    def read_audit(self) -> list[dict]:
        resp = self._table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :pfx)",
            ExpressionAttributeValues={":pk": self.incident_id, ":pfx": "AUDIT#"},
            ScanIndexForward=True,
        )
        return list(resp.get("Items", []))

    def _commit_marker_sk(self, key: str) -> str:
        return f"COMMIT#{key}"

    def is_committed(self, key: str) -> bool:
        """Return True if a COMMITTED record exists for `key`.

        Lookup order (cheapest first):
          1. O(1) GetItem on the dedicated COMMIT#{key} marker item written by claim_commit.
          2. GSI "KeyStatusIndex" (PK=key, SK=status) query, when provisioned.
          3. Linear scan of the partition's AUDIT items (last-resort fallback).
        """
        # 1) O(1) marker item
        try:
            resp = self._table.get_item(
                Key={"PK": self.incident_id, "SK": self._commit_marker_sk(key)}
            )
            if resp.get("Item"):
                return True
        except self._ClientError:
            pass

        # 2) GSI hot path
        try:
            resp = self._table.query(
                IndexName="KeyStatusIndex",
                KeyConditionExpression="#k = :key AND #s = :committed",
                ExpressionAttributeNames={"#k": "key", "#s": "status"},
                ExpressionAttributeValues={":key": key, ":committed": "COMMITTED", ":pk": self.incident_id},
                FilterExpression="PK = :pk",
                Select="COUNT",
            )
            return resp.get("Count", 0) > 0
        except self._ClientError:
            # 3) GSI not provisioned: fall back to linear scan
            return any(
                e.get("key") == key and e.get("status") == "COMMITTED"
                for e in self.read_audit()
            )

    # ---- atomic claim (exactly-once commit) ----

    def claim_commit(self, key: str, tool: str = "") -> bool:
        """Atomically claim the COMMITTED record for `key` via a conditional PutItem.

        Race-safety: a single conditional PutItem of the marker item SK="COMMIT#{key}"
        with ConditionExpression "attribute_not_exists(SK)" is atomic at the DynamoDB
        layer — only one concurrent writer can succeed. On ConditionalCheckFailedException
        the caller is a replay/loser and we return False. On success we ALSO append the
        normal AUDIT#<seq> COMMITTED record so the postmortem trail stays complete, then
        return True.
        """
        marker = {
            "PK": self.incident_id,
            "SK": self._commit_marker_sk(key),
            "status": "COMMITTED",
            "tool": tool,
            "key": key,
        }
        try:
            self._table.put_item(
                Item=marker,
                ConditionExpression="attribute_not_exists(SK)",
            )
        except self._ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False  # already claimed by another writer / replay
            raise
        # Won the claim — append the postmortem audit record too.
        self.append_audit({"status": "COMMITTED", "tool": tool, "key": key})
        return True

    # ---- reset ----

    def reset(self):
        """Delete all items for this incident from the table (state + audit)."""
        resp = self._table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": self.incident_id},
        )
        with self._table.batch_writer() as batch:
            for item in resp.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

def _make_backend(incident_id: str):
    backend = config.STATE_BACKEND
    if backend == "dynamodb":
        return DynamoDBBackend(incident_id)
    return FileBackend(incident_id)


# ---------------------------------------------------------------------------
# Public API — DurableState
# ---------------------------------------------------------------------------

class DurableState:
    """Append-only incident state machine, OUTSIDE the model provider.

    Delegates to a pluggable backend (FileBackend or DynamoDBBackend) selected
    by config.STATE_BACKEND. The public interface is identical regardless of backend.
    """

    def __init__(self, incident_id: str):
        self.incident_id = incident_id
        self._backend = _make_backend(incident_id)
        self.data = self._backend.load_state()

    def _flush(self):
        self._backend.save_state(self.data)

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

    # legacy path attribute so existing code/tests that reference .path still work
    @property
    def path(self) -> str:
        if isinstance(self._backend, FileBackend):
            return self._backend._state_path
        return f"dynamodb://{config.DYNAMODB_TABLE}/{self.incident_id}/STATE"


# ---------------------------------------------------------------------------
# Public API — AuditLog
# ---------------------------------------------------------------------------

class AuditLog:
    """Append-only audit trail. is_committed() enforces exactly-once.

    Delegates to the same backend as DurableState for the incident.
    """

    def __init__(self, incident_id: str):
        self.incident_id = incident_id
        self._backend = _make_backend(incident_id)

    def write(self, entry: dict):
        self._backend.append_audit(entry)

    def _entries(self) -> list[dict]:
        return self._backend.read_audit()

    def is_committed(self, key: str) -> bool:
        return self._backend.is_committed(key)

    def claim_commit(self, key: str, tool: str = "") -> bool:
        """Atomically record a COMMITTED entry for `key` iff none exists; race-safe.

        Returns True if this call won the claim (wrote the COMMITTED record), False if a
        COMMITTED record for `key` already existed (replay/loser). Delegates to the backend:
        FileBackend uses an exclusive fcntl flock; DynamoDBBackend uses a conditional PutItem.
        The written entry is {"status":"COMMITTED","tool":tool,"key":key} so postmortem,
        is_committed, and pending_keys keep working unchanged.
        """
        return self._backend.claim_commit(key, tool)

    def pending_keys(self) -> list[str]:
        entries = self._entries()
        committed = {e["key"] for e in entries if e.get("status") == "COMMITTED"}
        return [
            e["key"] for e in entries
            if e.get("status") == "PENDING" and e["key"] not in committed
        ]

    def postmortem(self) -> list[str]:
        return [
            f"{e.get('status', '?'):9} {e.get('tool', '')} key={e.get('key', '')}"
            for e in self._entries()
        ]

    # legacy path attribute
    @property
    def path(self) -> str:
        if isinstance(self._backend, FileBackend):
            return self._backend._audit_path
        return f"dynamodb://{config.DYNAMODB_TABLE}/{self.incident_id}/AUDIT"


# ---------------------------------------------------------------------------
# Module-level reset (works for both backends)
# ---------------------------------------------------------------------------

def reset(incident_id: str):
    """Remove all state and audit data for an incident. Works for both backends."""
    _make_backend(incident_id).reset()
