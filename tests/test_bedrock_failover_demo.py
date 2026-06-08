"""Tests: scripts/bedrock_failover_demo with a mocked boto3 transport (no live calls).

The live demo drives deadman.bedrock_ai.complete() against real AWS Bedrock. These tests
exercise the demo's failover logic and output shape entirely offline by monkeypatching
boto3.client('bedrock-runtime') — same technique as tests/test_bedrock_ai.py — so CI stays
green and never touches AWS.

Covers: chain composition (--down / --healthy), tier-by-tier walking, real-error
unwrapping/surfacing, the served/total-outage exit codes, --json and human output shape,
and config restoration after the runtime chain override.
"""
from __future__ import annotations

import importlib
import json

import pytest
from unittest.mock import MagicMock

import deadman.config as config
import deadman.bedrock_ai as bedrock_ai

demo = importlib.import_module("scripts.bedrock_failover_demo")


@pytest.fixture(autouse=True)
def _clear_client_cache():
    """bedrock_ai memoises clients per region; reset between tests for isolation."""
    bedrock_ai._clients.clear()
    yield
    bedrock_ai._clients.clear()


def _converse_response(text: str) -> dict:
    """Shape of a successful bedrock-runtime Converse response."""
    return {"output": {"message": {"content": [{"text": text}]}}}


def _access_denied(model_id: str) -> Exception:
    """A realistic AccessDeniedException-shaped error for a non-invocable model."""
    return RuntimeError(
        f"An error occurred (AccessDeniedException) when calling the Converse "
        f"operation: {model_id} is not available for this account."
    )


def _patch_boto3(monkeypatch, *, per_model=None, default=None):
    """Install a fake boto3.client('bedrock-runtime') driven by per-model outcomes.

    `per_model` maps modelId -> response dict | Exception; `default` is the fallthrough.
    """
    per_model = per_model or {}
    client = MagicMock()

    def _converse(modelId, **kwargs):  # noqa: N803 — boto3 uses camelCase kwargs
        outcome = per_model.get(modelId, default)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise RuntimeError(f"no canned outcome for {modelId}")
        return outcome

    client.converse.side_effect = _converse

    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    return client


# --------------------------------------------------------------------------- chain build


class TestBuildChain:

    def test_down_prepends_dead_tiers_then_real_chain(self):
        chain = demo.build_chain(down=2, healthy=False)
        real = config.BEDROCK_FALLBACK_CHAIN
        assert len(chain) == 2 + len(real)
        assert chain[0]["_induced_down"] is True
        assert chain[1]["_induced_down"] is True
        # Real tiers follow, not marked down, and carry the real model ids.
        assert chain[2].get("_induced_down") is not True
        assert chain[2]["model"] == real[0]["model"]

    def test_tier_indices_renumbered_contiguously(self):
        chain = demo.build_chain(down=3, healthy=False)
        assert [e["tier"] for e in chain] == list(range(len(chain)))

    def test_healthy_uses_unmodified_real_chain(self):
        chain = demo.build_chain(down=2, healthy=True)
        real = config.BEDROCK_FALLBACK_CHAIN
        assert len(chain) == len(real)
        assert all(not e.get("_induced_down") for e in chain)
        assert chain[0]["model"] == real[0]["model"]

    def test_dead_tier_uses_known_unavailable_model_ids(self):
        chain = demo.build_chain(down=2, healthy=False)
        assert chain[0]["model"] in demo.DOWN_MODEL_IDS
        assert chain[1]["model"] in demo.DOWN_MODEL_IDS

    def test_down_zero_is_effectively_healthy(self):
        chain = demo.build_chain(down=0, healthy=False)
        assert len(chain) == len(config.BEDROCK_FALLBACK_CHAIN)


# ------------------------------------------------------------------------ failover walk


class TestRunFailover:

    def test_dead_tiers_shed_to_first_healthy_tier(self, monkeypatch):
        real = config.BEDROCK_FALLBACK_CHAIN
        dead_id = demo.DOWN_MODEL_IDS[0]
        dead_id2 = demo.DOWN_MODEL_IDS[1]
        _patch_boto3(
            monkeypatch,
            per_model={
                dead_id: _access_denied(dead_id),
                dead_id2: _access_denied(dead_id2),
                real[0]["model"]: _converse_response("roll back the deploy"),
            },
        )

        chain = demo.build_chain(down=2, healthy=False)
        result = demo.run_failover(chain)

        assert result["served"] is True
        assert result["fallback_depth"] == 2
        assert result["winner"]["served_by"] == f'{real[0]["family"]}@{real[0]["region"]}'
        assert result["winner"]["text"] == "roll back the deploy"
        # Exactly three tiers attempted: two dead, then the winner; stops after success.
        assert len(result["attempts"]) == 3
        assert [a["served"] for a in result["attempts"]] == [False, False, True]

    def test_real_aws_error_is_unwrapped_not_the_outage_wrapper(self, monkeypatch):
        """Per-tier failures show the genuine AWS message, not 'all N tiers failed'."""
        real = config.BEDROCK_FALLBACK_CHAIN
        dead_id = demo.DOWN_MODEL_IDS[0]
        _patch_boto3(
            monkeypatch,
            per_model={
                dead_id: _access_denied(dead_id),
                real[0]["model"]: _converse_response("ok"),
            },
        )

        chain = demo.build_chain(down=1, healthy=False)
        result = demo.run_failover(chain)

        err = result["attempts"][0]["error"]
        assert "AccessDeniedException" in err
        assert "all 1 Bedrock tiers failed" not in err
        assert "last error:" not in err

    def test_healthy_path_tier0_answers_depth_zero(self, monkeypatch):
        real = config.BEDROCK_FALLBACK_CHAIN
        _patch_boto3(
            monkeypatch,
            per_model={real[0]["model"]: _converse_response("tier0 plan")},
        )

        chain = demo.build_chain(down=2, healthy=True)
        result = demo.run_failover(chain)

        assert result["served"] is True
        assert result["fallback_depth"] == 0
        assert len(result["attempts"]) == 1
        assert result["attempts"][0]["served"] is True

    def test_total_outage_when_every_tier_fails(self, monkeypatch):
        _patch_boto3(monkeypatch, default=RuntimeError("ServiceUnavailableException"))

        chain = demo.build_chain(down=2, healthy=False)
        result = demo.run_failover(chain)

        assert result["served"] is False
        assert result["winner"] is None
        assert result["fallback_depth"] is None
        assert len(result["attempts"]) == len(chain)
        assert all(not a["served"] for a in result["attempts"])

    def test_per_tier_records_have_expected_shape(self, monkeypatch):
        real = config.BEDROCK_FALLBACK_CHAIN
        _patch_boto3(
            monkeypatch,
            per_model={real[0]["model"]: _converse_response("ok")},
        )
        result = demo.run_failover(demo.build_chain(down=0, healthy=False))
        rec = result["attempts"][0]
        for key in ("tier", "label", "model", "served", "induced_down",
                    "served_by", "text", "error", "latency_ms"):
            assert key in rec

    def test_config_chain_restored_after_run(self, monkeypatch):
        """The runtime override must never permanently mutate config."""
        original = config.BEDROCK_FALLBACK_CHAIN
        real = config.BEDROCK_FALLBACK_CHAIN
        _patch_boto3(
            monkeypatch,
            per_model={real[0]["model"]: _converse_response("ok")},
        )
        demo.run_failover(demo.build_chain(down=2, healthy=False))
        assert config.BEDROCK_FALLBACK_CHAIN is original


# ------------------------------------------------------------------------------- main()


class TestMain:

    def test_exit_zero_on_served_answer(self, monkeypatch, capsys):
        real = config.BEDROCK_FALLBACK_CHAIN
        dead_id = demo.DOWN_MODEL_IDS[0]
        dead_id2 = demo.DOWN_MODEL_IDS[1]
        _patch_boto3(
            monkeypatch,
            per_model={
                dead_id: _access_denied(dead_id),
                dead_id2: _access_denied(dead_id2),
                real[0]["model"]: _converse_response("roll back"),
            },
        )

        rc = demo.main(["--down", "2"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "SERVED BY:" in out
        assert "final answer       : PRODUCED" in out
        assert "FAILED:" in out

    def test_exit_nonzero_on_total_outage(self, monkeypatch, capsys):
        _patch_boto3(monkeypatch, default=RuntimeError("ServiceUnavailableException"))

        rc = demo.main(["--down", "2"])
        out = capsys.readouterr().out

        assert rc == 1
        assert "TOTAL OUTAGE" in out

    def test_json_output_is_valid_and_machine_readable(self, monkeypatch, capsys):
        real = config.BEDROCK_FALLBACK_CHAIN
        dead_id = demo.DOWN_MODEL_IDS[0]
        _patch_boto3(
            monkeypatch,
            per_model={
                dead_id: _access_denied(dead_id),
                real[0]["model"]: _converse_response("roll back"),
            },
        )

        rc = demo.main(["--down", "1", "--json"])
        out = capsys.readouterr().out
        payload = json.loads(out)

        assert rc == 0
        assert payload["served"] is True
        assert payload["fallback_depth"] == 1
        assert payload["mode"] == "induced_outage"
        assert payload["down"] == 1
        assert payload["region"] == config.AWS_REGION
        assert payload["attempts"][0]["served"] is False
        assert payload["attempts"][1]["served"] is True

    def test_healthy_flag_runs_unmodified_chain(self, monkeypatch, capsys):
        real = config.BEDROCK_FALLBACK_CHAIN
        _patch_boto3(
            monkeypatch,
            per_model={real[0]["model"]: _converse_response("tier0")},
        )

        rc = demo.main(["--healthy", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["mode"] == "healthy"
        assert payload["down"] == 0
        assert payload["fallback_depth"] == 0
        assert len(payload["attempts"]) == 1

    def test_negative_down_is_rejected(self):
        with pytest.raises(SystemExit):
            demo.main(["--down", "-1"])

    def test_weird_error_does_not_crash_surfaced_in_record(self, monkeypatch, capsys):
        """A non-boto3 surprise error is caught per-tier and surfaced, never raised raw."""
        real = config.BEDROCK_FALLBACK_CHAIN
        dead_id = demo.DOWN_MODEL_IDS[0]
        _patch_boto3(
            monkeypatch,
            per_model={
                dead_id: ValueError("totally unexpected boto internal"),
                real[0]["model"]: _converse_response("recovered"),
            },
        )

        rc = demo.main(["--down", "1"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "totally unexpected boto internal" in out
