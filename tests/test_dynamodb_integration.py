"""DynamoDB integration tests — real DynamoDBBackend via moto.

Exercises production paths that unit tests only mock:
  - State round-trip (set_pending / commit / note) across a fresh instance.
  - Audit append + monotonic ordering.
  - claim_commit atomicity: exactly-once conditional PutItem.
  - is_committed via the COMMIT# marker (O(1)) and the GSI path.
  - Exactly-once end-to-end with DynamoDB: kill mid-revert, fresh Deadman resumes,
    revert reconciled exactly once, audit shows one COMMITTED for the key.
  - reset() removes all items (state + audit + COMMIT marker).
  - TTL attribute written on the STATE item when DEADMAN_STATE_TTL_SECONDS > 0.

Requires moto[dynamodb]. If moto is absent the whole module is skipped cleanly.
"""
from __future__ import annotations

import os

import pytest

moto = pytest.importorskip("moto")

import boto3  # noqa: E402  (only reached after importorskip)
from moto import mock_aws  # noqa: E402

import deadman.config as config  # noqa: E402
import deadman.state as state_module  # noqa: E402
from deadman.state import DurableState, AuditLog, DynamoDBBackend  # noqa: E402


# ---------------------------------------------------------------------------
# Table schema constants — must match state.py exactly
# ---------------------------------------------------------------------------

_TABLE_NAME = "deadman-test-incidents"
_GSI_NAME = "KeyStatusIndex"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_table(dynamodb):
    """Create the DynamoDB table with the schema that DynamoDBBackend expects."""
    dynamodb.create_table(
        TableName=_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "key", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": _GSI_NAME,
                "KeySchema": [
                    {"AttributeName": "key", "KeyType": "HASH"},
                    {"AttributeName": "status", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb.Table(_TABLE_NAME)


def _patch_dynamo(monkeypatch):
    """Point config at the test table and the fake AWS environment."""
    monkeypatch.setattr(config, "STATE_BACKEND", "dynamodb")
    monkeypatch.setattr(config, "DYNAMODB_TABLE", _TABLE_NAME)
    monkeypatch.setattr(config, "AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


# ---------------------------------------------------------------------------
# Module-scoped moto context + table so we only provision once across tests
# that share it via the per-test fixture.
# ---------------------------------------------------------------------------

@pytest.fixture()
def dynamo_table(monkeypatch):
    """Per-test fixture: fresh moto mock_aws context + table + config patches."""
    _patch_dynamo(monkeypatch)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = _create_table(ddb)
        yield table


# ---------------------------------------------------------------------------
# 1. State round-trip across a fresh DynamoDBBackend instance
# ---------------------------------------------------------------------------

class TestDynamoDBStateRoundTrip:

    def test_set_pending_visible_from_new_instance(self, dynamo_table, isolated_state):
        """set_pending persists to DynamoDB and is visible from a new DynamoDBBackend."""
        inc = "ddb-roundtrip-pending"
        ds = DurableState(inc)
        ds.set_pending("github.revert_pr", "key-abc")

        # Simulate a fresh process by constructing a brand-new DurableState
        ds2 = DurableState(inc)
        assert ds2.pending is not None
        assert ds2.pending["action"] == "github.revert_pr"
        assert ds2.pending["key"] == "key-abc"

    def test_commit_persists_and_clears_pending(self, dynamo_table, isolated_state):
        """commit() clears pending and records in actions_committed across a new instance."""
        inc = "ddb-roundtrip-commit"
        ds = DurableState(inc)
        ds.set_pending("github.revert_pr", "key-commit")
        ds.commit("github.revert_pr", "key-commit")

        ds2 = DurableState(inc)
        assert ds2.pending is None
        assert len(ds2.data["actions_committed"]) == 1
        assert ds2.data["actions_committed"][0]["action"] == "github.revert_pr"

    def test_note_appends_timeline_across_instances(self, dynamo_table, isolated_state):
        """note() appends to timeline and survives a fresh instance construction."""
        inc = "ddb-roundtrip-note"
        ds = DurableState(inc)
        ds.note("step alpha")
        ds.note("step beta")

        ds2 = DurableState(inc)
        assert "step alpha" in ds2.data["timeline"]
        assert "step beta" in ds2.data["timeline"]

    def test_fresh_instance_has_empty_state(self, dynamo_table, isolated_state):
        """Brand-new DurableState for an unknown incident returns empty defaults."""
        inc = "ddb-roundtrip-empty"
        ds = DurableState(inc)
        assert ds.pending is None
        assert ds.data["actions_committed"] == []
        assert ds.data["timeline"] == []


# ---------------------------------------------------------------------------
# 2. Audit append + monotonic ordering
# ---------------------------------------------------------------------------

class TestDynamoDBAuditOrdering:

    def test_audit_entries_are_monotonically_ordered(self, dynamo_table, isolated_state):
        """Multiple appended audit entries arrive back in insertion order."""
        inc = "ddb-audit-order"
        audit = AuditLog(inc)
        for i in range(5):
            audit.write({"status": "PENDING", "tool": f"tool-{i}", "key": f"key-{i}"})
        audit.write({"status": "COMMITTED", "tool": "tool-4", "key": "key-4"})

        entries = audit._entries()
        # There should be 6 entries and the sort keys should be in ascending order
        assert len(entries) == 6
        sks = [e["SK"] for e in entries]
        assert sks == sorted(sks), f"Audit entries not monotonically ordered: {sks}"

    def test_seq_numbers_zero_padded(self, dynamo_table, isolated_state):
        """Audit SK format is AUDIT#<12-digit-zero-padded-seq>."""
        inc = "ddb-audit-seq"
        audit = AuditLog(inc)
        audit.write({"status": "PENDING", "tool": "t", "key": "k"})

        entries = audit._entries()
        assert len(entries) == 1
        sk = entries[0]["SK"]
        assert sk.startswith("AUDIT#")
        seq_part = sk[len("AUDIT#"):]
        assert len(seq_part) == 12, f"Seq not zero-padded 12 digits: {seq_part!r}"
        assert seq_part == "000000000000"

    def test_multiple_appends_increment_seq(self, dynamo_table, isolated_state):
        """Second append gets seq 1, third gets seq 2, etc."""
        inc = "ddb-audit-incr"
        audit = AuditLog(inc)
        audit.write({"status": "PENDING", "tool": "t", "key": "k1"})
        audit.write({"status": "COMMITTED", "tool": "t", "key": "k1"})
        audit.write({"status": "PENDING", "tool": "t2", "key": "k2"})

        entries = audit._entries()
        expected_sks = [
            "AUDIT#000000000000",
            "AUDIT#000000000001",
            "AUDIT#000000000002",
        ]
        actual_sks = [e["SK"] for e in entries]
        assert actual_sks == expected_sks, f"Unexpected SK sequence: {actual_sks}"


# ---------------------------------------------------------------------------
# 3. claim_commit atomicity: first call True, replay False; marker exists; is_committed True
# ---------------------------------------------------------------------------

class TestDynamoDBClaimCommit:

    def test_first_claim_returns_true(self, dynamo_table, isolated_state):
        """First claim_commit call returns True."""
        inc = "ddb-claim-first"
        audit = AuditLog(inc)
        result = audit.claim_commit("key-first", tool="github.revert_pr")
        assert result is True

    def test_replay_returns_false(self, dynamo_table, isolated_state):
        """Second claim_commit for the same key returns False (replay / already claimed)."""
        inc = "ddb-claim-replay"
        audit = AuditLog(inc)
        first = audit.claim_commit("key-replay", tool="github.revert_pr")
        second = audit.claim_commit("key-replay", tool="github.revert_pr")
        assert first is True
        assert second is False

    def test_exactly_one_commit_marker_item(self, dynamo_table, isolated_state):
        """Exactly one COMMIT#key marker item is present after claim_commit (even after replay)."""
        inc = "ddb-claim-marker"
        audit = AuditLog(inc)
        audit.claim_commit("key-marker", tool="t")
        audit.claim_commit("key-marker", tool="t")  # replay — should not write a second marker

        # Directly inspect the table for COMMIT#key-marker items
        resp = dynamo_table.query(
            KeyConditionExpression="PK = :pk AND SK = :sk",
            ExpressionAttributeValues={":pk": inc, ":sk": "COMMIT#key-marker"},
        )
        assert resp["Count"] == 1, (
            f"Expected exactly 1 COMMIT marker, found {resp['Count']}"
        )

    def test_is_committed_true_after_claim(self, dynamo_table, isolated_state):
        """is_committed returns True for a key that was successfully claimed."""
        inc = "ddb-claim-is-committed"
        audit = AuditLog(inc)
        audit.claim_commit("key-ic", tool="github.revert_pr")
        assert audit.is_committed("key-ic") is True

    def test_is_committed_false_before_claim(self, dynamo_table, isolated_state):
        """is_committed returns False for a key that was never claimed."""
        inc = "ddb-claim-not-committed"
        audit = AuditLog(inc)
        assert audit.is_committed("key-never") is False

    def test_claim_also_writes_audit_record(self, dynamo_table, isolated_state):
        """A successful claim_commit also appends an AUDIT#seq COMMITTED record."""
        inc = "ddb-claim-audit-record"
        audit = AuditLog(inc)
        audit.claim_commit("key-audit-check", tool="github.revert_pr")

        entries = audit._entries()
        committed = [
            e for e in entries
            if e.get("status") == "COMMITTED" and e.get("key") == "key-audit-check"
        ]
        assert len(committed) == 1, (
            f"Expected 1 COMMITTED AUDIT record, got {len(committed)}"
        )


# ---------------------------------------------------------------------------
# 4. is_committed: COMMIT# marker (O(1)) path and GSI path
# ---------------------------------------------------------------------------

class TestDynamoDBIsCommitted:

    def test_marker_item_path(self, dynamo_table, isolated_state):
        """is_committed resolves via the O(1) COMMIT# GetItem path (marker exists)."""
        inc = "ddb-iscommitted-marker"
        backend = DynamoDBBackend(inc)

        # Write the COMMIT marker directly to simulate the claim_commit winner
        marker_sk = "COMMIT#the-key"
        dynamo_table.put_item(
            Item={"PK": inc, "SK": marker_sk, "status": "COMMITTED", "key": "the-key", "tool": "t"}
        )

        assert backend.is_committed("the-key") is True

    def test_marker_not_present_returns_false(self, dynamo_table, isolated_state):
        """is_committed returns False when neither marker nor GSI entry exists."""
        inc = "ddb-iscommitted-absent"
        backend = DynamoDBBackend(inc)
        assert backend.is_committed("absent-key") is False

    def test_gsi_path_when_marker_absent_but_audit_present(self, dynamo_table, isolated_state):
        """is_committed can also be confirmed via the GSI (audit record has key+status)."""
        inc = "ddb-iscommitted-gsi"
        audit = AuditLog(inc)
        # Write an audit record with key + status=COMMITTED (simulates legacy data without marker)
        audit.write({"status": "COMMITTED", "tool": "github.revert_pr", "key": "gsi-key"})

        # The backend GSI / linear scan fallback must find it
        backend = DynamoDBBackend(inc)
        assert backend.is_committed("gsi-key") is True

    def test_is_committed_from_fresh_instance(self, dynamo_table, isolated_state):
        """is_committed works from a completely fresh DynamoDBBackend instance."""
        inc = "ddb-iscommitted-fresh"
        audit1 = AuditLog(inc)
        audit1.claim_commit("fresh-key", tool="t")

        # New instance — simulates a fresh process
        audit2 = AuditLog(inc)
        assert audit2.is_committed("fresh-key") is True


# ---------------------------------------------------------------------------
# 5. Exactly-once end-to-end on DynamoDB
#    Kill mid-revert, fresh Deadman resumes, exactly one COMMITTED in audit
# ---------------------------------------------------------------------------

class TestDynamoDBExactlyOnce:

    def test_kill_resume_exactly_once(self, dynamo_table, isolated_state):
        """Kill mid-revert on DynamoDB backend → fresh Deadman reconciles → world count == 1."""
        from deadman.commander import Deadman, action_key
        from deadman.chaos import Chaos
        from deadman.world import World
        from deadman.mcp_gateway import KillSignal

        inc = "ddb-eo-kill-resume"
        state_module.reset(inc)

        world = World()
        revert_key = action_key(inc, "revert_pr", "PR-1337")

        chaos = Chaos()
        chaos.kill_process_after(revert_key)

        # First run: killed mid-revert
        agent1 = Deadman(inc, world, chaos)
        with pytest.raises(KillSignal):
            agent1.run()

        # Side effect happened exactly once before the kill
        assert world.count("revert_pr") == 1

        # Fresh Deadman: no chaos, resumes from DynamoDB state
        chaos.kill_after = None
        agent2 = Deadman(inc, world, chaos)
        sb = agent2.run(resume=True)

        assert world.count("revert_pr") == 1, (
            f"EXACTLY-ONCE VIOLATED on DynamoDB backend: "
            f"revert_pr ran {world.count('revert_pr')} times. Notes: {sb.notes}"
        )
        assert sb.survived is True

    def test_audit_has_exactly_one_committed_revert(self, dynamo_table, isolated_state):
        """After kill+resume on DynamoDB, postmortem shows exactly one COMMITTED revert."""
        from deadman.commander import Deadman, action_key
        from deadman.chaos import Chaos
        from deadman.world import World
        from deadman.mcp_gateway import KillSignal

        inc = "ddb-eo-audit-check"
        state_module.reset(inc)

        world = World()
        revert_key = action_key(inc, "revert_pr", "PR-1337")
        chaos = Chaos()
        chaos.kill_process_after(revert_key)

        agent1 = Deadman(inc, world, chaos)
        with pytest.raises(KillSignal):
            agent1.run()

        chaos.kill_after = None
        agent2 = Deadman(inc, world, chaos)
        agent2.run(resume=True)

        audit = AuditLog(inc)
        postmortem = audit.postmortem()
        committed_reverts = [
            line for line in postmortem
            if "COMMITTED" in line and "revert_pr" in line
        ]
        assert len(committed_reverts) == 1, (
            f"Expected exactly 1 COMMITTED revert_pr in postmortem, "
            f"got {len(committed_reverts)}. Full postmortem: {postmortem}"
        )


# ---------------------------------------------------------------------------
# 6. reset() deletes all items (state + audit + COMMIT marker)
# ---------------------------------------------------------------------------

class TestDynamoDBReset:

    def test_reset_removes_all_items(self, dynamo_table, isolated_state):
        """reset() deletes STATE, all AUDIT#, and all COMMIT# items for the incident."""
        inc = "ddb-reset-all"

        # Write state
        ds = DurableState(inc)
        ds.set_pending("t", "k")

        # Write audit + claim
        audit = AuditLog(inc)
        audit.write({"status": "PENDING", "tool": "t", "key": "k"})
        audit.claim_commit("k", tool="t")

        # Confirm items exist
        resp = dynamo_table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": inc},
        )
        assert resp["Count"] >= 3, (
            f"Expected at least 3 items before reset, got {resp['Count']}"
        )

        # Reset
        state_module.reset(inc)

        # All gone
        resp2 = dynamo_table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": inc},
        )
        assert resp2["Count"] == 0, (
            f"Expected 0 items after reset, got {resp2['Count']}"
        )

    def test_reset_leaves_other_incidents_intact(self, dynamo_table, isolated_state):
        """reset() only touches the target incident; other incidents are unaffected."""
        inc_a = "ddb-reset-a"
        inc_b = "ddb-reset-b"

        for inc in (inc_a, inc_b):
            ds = DurableState(inc)
            ds.set_pending("t", "k")

        state_module.reset(inc_a)

        # inc_b should still have its STATE item
        resp = dynamo_table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": inc_b},
        )
        assert resp["Count"] >= 1, (
            f"Other incident items deleted by reset: {resp['Count']} items left"
        )


# ---------------------------------------------------------------------------
# 7. TTL attribute on the STATE item
# ---------------------------------------------------------------------------

class TestDynamoDBTTL:
    """TTL tests spin up their own isolated mock_aws context to avoid table name collision."""

    def test_ttl_attribute_present_when_enabled(self, monkeypatch, isolated_state):
        """STATE item has a 'ttl' attribute when DEADMAN_STATE_TTL_SECONDS > 0."""
        import time as _time

        _patch_dynamo(monkeypatch)
        monkeypatch.setenv("DEADMAN_STATE_TTL_SECONDS", "3600")
        monkeypatch.setattr(state_module, "_TTL_SECONDS", 3600)

        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            table = _create_table(ddb)

            inc = "ddb-ttl-enabled"
            before = int(_time.time())
            ds = DurableState(inc)
            ds.set_pending("t", "k")

            resp = table.get_item(Key={"PK": inc, "SK": "STATE"})
            item = resp.get("Item")
            assert item is not None, "STATE item not found"
            assert "ttl" in item, f"TTL attribute missing from STATE item: {item}"
            ttl_val = int(item["ttl"])
            assert ttl_val > before + 3500, (
                f"TTL value {ttl_val} does not look ~3600s from now (before={before})"
            )

    def test_ttl_attribute_absent_when_disabled(self, monkeypatch, isolated_state):
        """STATE item has NO 'ttl' attribute when DEADMAN_STATE_TTL_SECONDS=0."""
        _patch_dynamo(monkeypatch)
        monkeypatch.setenv("DEADMAN_STATE_TTL_SECONDS", "0")
        monkeypatch.setattr(state_module, "_TTL_SECONDS", 0)

        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            table = _create_table(ddb)

            inc = "ddb-ttl-disabled"
            ds = DurableState(inc)
            ds.set_pending("t", "k")

            resp = table.get_item(Key={"PK": inc, "SK": "STATE"})
            item = resp.get("Item")
            assert item is not None, "STATE item not found"
            assert "ttl" not in item, (
                f"TTL attribute should be absent when TTL_SECONDS=0, got: {item}"
            )
