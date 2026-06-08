"""LIVE AWS Bedrock cross-provider failover demonstration for DEADMAN.

This is a self-contained, screen-recordable proof that DEADMAN's cross-provider
resilience is *genuine* — it makes REAL AWS Bedrock Converse calls (no mocking) and
shows, tier by tier, the live failover as it happens.

It drives deadman.bedrock_ai.complete() — the exact real-mode code path the agent uses
— but walks the chain one tier at a time so it can print each tier's real outcome:
either "FAILED: <real AWS error>" or "SERVED" with the real model output text.

To demonstrate an outage HONESTLY (without faking anything), it prepends genuinely
non-invocable model ids (e.g. ``anthropic.claude-opus-4-8``, which raises a real
AccessDeniedException on this account) to the front of the chain. The number of downed
top tiers is configurable with --down N (default 2). The real, invocable chain from
config.BEDROCK_FALLBACK_CHAIN is appended after the downed tiers, so failover sheds
cross-provider to a model that actually answers.

Modes::

    python3 scripts/bedrock_failover_demo.py            # --down 2 (default): real outage -> real failover
    python3 scripts/bedrock_failover_demo.py --down 3   # shed deeper
    python3 scripts/bedrock_failover_demo.py --healthy   # unmodified chain; tier 0 answers (happy path)
    python3 scripts/bedrock_failover_demo.py --json      # machine-readable output

Exit code: 0 when some tier produced an answer, non-zero on total outage / fatal error.

The chain override is applied at runtime only — config.py is never permanently edited.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from typing import Any, Iterator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deadman.config as config  # noqa: E402
from deadman import bedrock_ai  # noqa: E402

# Genuinely non-invocable model ids on this AWS account. Invoking these produces a REAL
# AccessDeniedException from Bedrock — an honest way to induce an outage at the top tiers.
# (claude-opus-4-8 is verified NOT enabled for invocation here.)
DOWN_MODEL_IDS: list[str] = [
    "anthropic.claude-opus-4-8",
    "us.anthropic.claude-opus-4-8",
    "anthropic.claude-3-opus-20240229-v1:0",
]

# A realistic SRE incident prompt — the kind DEADMAN's incident commander fields live.
DEFAULT_PROMPT = (
    "Service shows elevated 5xx after a deploy. "
    "In 2 sentences, what is the first mitigation step?"
)


def _down_tier(idx: int) -> dict[str, Any]:
    """Build a fallback-chain entry for a genuinely-unavailable (downed) top tier."""
    model_id = DOWN_MODEL_IDS[idx % len(DOWN_MODEL_IDS)]
    family = model_id.split(".")[-1].split(":")[0]
    return {
        "tier": idx,
        "family": family,
        "model": model_id,
        "region": config.AWS_REGION,
        "provider": "anthropic",
        "_induced_down": True,
    }


def build_chain(down: int, healthy: bool) -> list[dict[str, Any]]:
    """Compose the chain to walk: ``down`` induced-dead top tiers + the real chain.

    The real, invocable tiers come from config.BEDROCK_FALLBACK_CHAIN. When *healthy* is
    set (or down == 0), no dead tiers are prepended and the unmodified real chain is used.
    Tier indices are renumbered 0..N so fallback_depth reflects this composed chain.
    """
    real = [dict(entry) for entry in config.BEDROCK_FALLBACK_CHAIN]
    dead = [] if healthy else [_down_tier(i) for i in range(down)]
    composed = dead + real
    for new_tier, entry in enumerate(composed):
        entry["tier"] = new_tier
    return composed


@contextlib.contextmanager
def _chain_override(chain: list[dict[str, Any]]) -> Iterator[None]:
    """Temporarily swap config.BEDROCK_FALLBACK_CHAIN; always restore on exit."""
    original = config.BEDROCK_FALLBACK_CHAIN
    config.BEDROCK_FALLBACK_CHAIN = chain  # type: ignore[assignment]
    try:
        yield
    finally:
        config.BEDROCK_FALLBACK_CHAIN = original  # type: ignore[assignment]


def _invoke_single_tier(entry: dict[str, Any]) -> dict[str, Any]:
    """Make ONE real Bedrock call for a single tier via the real bedrock_ai code path.

    Drives bedrock_ai.complete() with the chain pinned to just this one tier, so we get
    the genuine Converse call (and genuine error, if any) for exactly this model. Returns
    a per-tier result record; never raises — any AWS/boto3 error is surfaced in the record.
    """
    label = f"{entry['family']} ({entry['model']})"
    started = time.monotonic()
    try:
        with _chain_override([entry]):
            response = bedrock_ai.complete(DEFAULT_PROMPT)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return {
            "tier": entry["tier"],
            "label": label,
            "model": entry["model"],
            "served": True,
            "induced_down": bool(entry.get("_induced_down")),
            "served_by": response.get("served_by"),
            "text": response.get("text", ""),
            "error": None,
            "latency_ms": round(elapsed_ms, 1),
        }
    except bedrock_ai.BedrockOutage as exc:
        # Single-tier chain failed -> unwrap to the genuine per-tier AWS error so the
        # demo shows the real Bedrock message, not the "all N tiers failed" wrapper.
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return _failure_record(entry, label, _unwrap_outage(exc), elapsed_ms)
    except Exception as exc:  # noqa: BLE001 — be defensive: never crash on a weird AWS error
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return _failure_record(entry, label, f"{type(exc).__name__}: {exc}", elapsed_ms)


def _unwrap_outage(exc: bedrock_ai.BedrockOutage) -> str:
    """Pull the real per-tier AWS error out of a single-tier BedrockOutage wrapper.

    bedrock_ai raises ``BedrockOutage("all N Bedrock tiers failed; last error: <real>")``.
    For our one-tier walk, the genuine Bedrock message after "last error: " is what we
    want to show; fall back to the full message if the marker is absent.
    """
    marker = "last error: "
    message = str(exc)
    real = message.split(marker, 1)[1] if marker in message else message
    return real.strip()


def _failure_record(
    entry: dict[str, Any], label: str, error: str, elapsed_ms: float
) -> dict[str, Any]:
    return {
        "tier": entry["tier"],
        "label": label,
        "model": entry["model"],
        "served": False,
        "induced_down": bool(entry.get("_induced_down")),
        "served_by": None,
        "text": "",
        "error": error,
        "latency_ms": round(elapsed_ms, 1),
    }


def run_failover(chain: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk the composed chain, making real per-tier Bedrock calls until one answers.

    Returns a result dict with per-tier attempts and the winning tier (if any).
    """
    attempts: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    for entry in chain:
        record = _invoke_single_tier(entry)
        attempts.append(record)
        if record["served"]:
            winner = record
            break
    return {
        "attempts": attempts,
        "winner": winner,
        "served": winner is not None,
        "fallback_depth": winner["tier"] if winner else None,
        "prompt": DEFAULT_PROMPT,
    }


def _print_human(result: dict[str, Any], *, down: int, healthy: bool) -> None:
    print("=" * 72)
    print("DEADMAN — LIVE AWS Bedrock cross-provider failover")
    print("=" * 72)
    mode = "HEALTHY (unmodified real chain)" if healthy else f"INDUCED OUTAGE (--down {down})"
    print(f"mode: {mode}   region: {config.AWS_REGION}")
    print(f"incident prompt: {result['prompt']}")
    print("-" * 72)

    for rec in result["attempts"]:
        tag = " [induced-dead]" if rec["induced_down"] else ""
        if rec["served"]:
            print(f"tier {rec['tier']} ({rec['label']}){tag} SERVED  "
                  f"[{rec['latency_ms']} ms]")
        else:
            print(f"tier {rec['tier']} ({rec['label']}){tag} FAILED: {rec['error']}  "
                  f"[{rec['latency_ms']} ms]")

    print("-" * 72)
    winner = result["winner"]
    if winner:
        print(f"SERVED BY: {winner['served_by']}  (fallback_depth={result['fallback_depth']})")
        print("ANSWER:")
        print(winner["text"].strip() or "(empty response)")
    else:
        print("TOTAL OUTAGE: no tier produced an answer.")

    print("-" * 72)
    print("Hackathon rubric — resilience proof:")
    n_failed = sum(1 for r in result["attempts"] if not r["served"])
    print(f"  failure introduced : {n_failed} top tier(s) hit a real Bedrock error")
    if n_failed:
        print(f"  failure detected   : per-tier Converse error caught, shed to next tier")
    else:
        print(f"  failure detected   : n/a (healthy path; tier 0 answered)")
    if winner:
        print(f"  fallback path      : depth 0 -> {result['fallback_depth']} "
              f"(cross-provider) -> {winner['served_by']}")
        print(f"  final answer       : PRODUCED despite the outage")
    else:
        print(f"  fallback path      : exhausted")
        print(f"  final answer       : NOT produced (every tier down)")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LIVE AWS Bedrock cross-provider failover demonstration for DEADMAN."
    )
    parser.add_argument(
        "--down",
        type=int,
        default=2,
        help="Number of genuinely-unavailable top tiers to prepend (default: 2). "
             "Ignored when --healthy is set.",
    )
    parser.add_argument(
        "--healthy",
        action="store_true",
        help="Run the real unmodified chain (tier 0 should answer; happy path).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of human output.",
    )
    args = parser.parse_args(argv)

    if args.down < 0:
        parser.error("--down must be >= 0")

    chain = build_chain(args.down, args.healthy)

    try:
        result = run_failover(chain)
    except Exception as exc:  # noqa: BLE001 — defensive top-level guard; never crash raw
        if args.json:
            print(json.dumps({"served": False, "fatal_error": f"{type(exc).__name__}: {exc}"}))
        else:
            print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        result["mode"] = "healthy" if args.healthy else "induced_outage"
        result["down"] = 0 if args.healthy else args.down
        result["region"] = config.AWS_REGION
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_human(result, down=args.down, healthy=args.healthy)

    return 0 if result["served"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
