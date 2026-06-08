"""Auto-drive the running DEADMAN web server for a clean screen recording.

This hits the live HTTP endpoints in the same order a presenter would click the
war-room UI, printing a friendly narrated line (and pausing) before each step so
the recording is watchable. It is a *driver*, not a test: it exercises the real
server so the split-screen UI and SSE timeline update on camera while you talk.

Sequence
--------
    1. POST /api/chaos/reset                  — clean slate
    2. POST /api/chaos/correlated_blackout    — introduce the regional outage
    3. POST /api/chaos/kill_bedrock           — all Bedrock tiers down -> AUTO-LEASH
    4. POST /api/chaos/rate_limit_storm       — 429 storm -> latency shedding
    5. POST /api/chaos/corrupt_output         — garbage JSON -> Post-Tool guardrail
    6. POST /api/chaos/kill_mid_rollback      — SIGKILL between side-effect + COMMIT
    7. POST /api/demo/run                      — run NAIVE vs DEADMAN, print headline
    8. GET  /metrics                           — show observability surface

Run (with the server already up on :8080):
    python3 scripts/demo_drive.py
    python3 scripts/demo_drive.py --base-url http://localhost:8090 --pause 3

Uses only the Python standard library (no new dependencies).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# Chaos toggles fired in presenter order. Each entry is (toggle, narration).
# These match deadman.webhook._VALID_TOGGLES exactly.
_CHAOS_STEPS: list[tuple[str, str]] = [
    ("reset", "Resetting chaos to a clean slate so the war room starts green."),
    ("correlated_blackout",
     "Introducing a CORRELATED BLACKOUT — us-east-1 region + provider + tools degrade at once."),
    ("kill_bedrock",
     "Killing ALL Bedrock tiers — the brain degrades, so the agent AUTO-LEASHES its own destructive authority."),
    ("rate_limit_storm",
     "Firing a 429 STORM on tier-0 — the AI Gateway sheds latency and fails over to a healthy tier."),
    ("corrupt_output",
     "Injecting CORRUPT OUTPUT — a degraded tool returns garbage JSON; the Post-Tool guardrail catches it."),
    ("kill_mid_rollback",
     "Arming KILL mid-rollback — SIGKILL lands between the side-effect and its COMMIT."),
]


def _say(message: str, pause: float) -> None:
    """Print a narrated step line, then pause so the recording can breathe."""
    print(f"\n>>> {message}")
    sys.stdout.flush()
    if pause > 0:
        time.sleep(pause)


def _request(base_url: str, path: str, method: str) -> Any:
    """Make one HTTP request and return the decoded JSON body (or raw text)."""
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (localhost demo)
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _print_offline_help(base_url: str, exc: Exception) -> None:
    """Explain how to start the server when we cannot reach it."""
    print(f"\n[ERROR] Could not reach the DEADMAN server at {base_url}", file=sys.stderr)
    print(f"        ({type(exc).__name__}: {exc})", file=sys.stderr)
    print("\nStart it first, then re-run this driver:", file=sys.stderr)
    print("    uvicorn deadman.webhook:app --host 0.0.0.0 --port 8080", file=sys.stderr)
    print("\nThen, in another terminal:", file=sys.stderr)
    print("    python3 scripts/demo_drive.py", file=sys.stderr)
    print(
        "\nIf you started uvicorn on a different port, pass it:\n"
        "    python3 scripts/demo_drive.py --base-url http://localhost:<PORT>",
        file=sys.stderr,
    )


def _check_server(base_url: str) -> bool:
    """Probe /healthz so we fail fast with a clear message if the server is down."""
    try:
        health = _request(base_url, "/healthz", "GET")
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as exc:
        _print_offline_help(base_url, exc)
        return False
    mode = health.get("mode") if isinstance(health, dict) else "?"
    print(f"[ok] Server is up at {base_url} (mode={mode}).")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-drive the running DEADMAN web server for a screen recording."
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8080",
        help="Base URL of the running server (default: http://localhost:8080).",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=2.0,
        help="Seconds to pause before each step so the recording is watchable (default: 2.0).",
    )
    args = parser.parse_args()

    base_url: str = args.base_url
    pause: float = args.pause

    print("=" * 72)
    print("  DEADMAN — auto-driving the war room for a clean recording")
    print("=" * 72)
    print(f"  base-url = {base_url}   pause = {pause}s/step")
    print("  Tip: have the UI open at this URL on screen while this runs.")

    if not _check_server(base_url):
        return 1

    # 1) Fire each chaos toggle in presenter order.
    for toggle, narration in _CHAOS_STEPS:
        _say(narration, pause)
        try:
            result = _request(base_url, f"/api/chaos/{toggle}", "POST")
        except urllib.error.HTTPError as exc:
            print(f"    [warn] POST /api/chaos/{toggle} -> HTTP {exc.code}: {exc.reason}")
            continue
        except (urllib.error.URLError, OSError) as exc:
            _print_offline_help(base_url, exc)
            return 1
        if isinstance(result, dict) and "chaos" in result:
            print(f"    chaos state -> {json.dumps(result['chaos'])}")
        else:
            print(f"    response -> {result}")

    # 2) Run the deterministic NAIVE-vs-DEADMAN comparison.
    _say(
        "Running the demo: NAIVE agent vs DEADMAN under every fault at once.",
        pause,
    )
    try:
        demo = _request(base_url, "/api/demo/run", "POST")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        print(f"    [warn] POST /api/demo/run failed: {exc}")
        demo = None

    if isinstance(demo, dict):
        head = demo.get("headline", {})
        naive = demo.get("naive", {})
        dead = demo.get("deadman", {})
        print(f"    NAIVE   : survived={naive.get('survived')} "
              f"double_executions={head.get('double_executions_naive')}")
        print(f"    DEADMAN : survived={dead.get('survived')} "
              f"double_executions={head.get('double_executions_deadman')} "
              f"guardrail_blocks={dead.get('guardrail_blocks')} "
              f"drain_authority={dead.get('drain_authority')}")
        print("    HEADLINE: NAIVE double-executes the rollback; DEADMAN is exactly-once.")

    # 3) Show the observability surface.
    _say("Showing the /metrics observability surface.", pause)
    try:
        metrics = _request(base_url, "/metrics", "GET")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        print(f"    [warn] GET /metrics failed: {exc}")
        metrics = ""
    text = metrics if isinstance(metrics, str) else json.dumps(metrics)
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    preview = lines[:6] if lines else ["(no metric samples yet)"]
    for ln in preview:
        print(f"    {ln}")
    if len(lines) > len(preview):
        print(f"    ... ({len(lines)} metric lines total)")

    print("\n" + "=" * 72)
    print("  Drive complete. Stop the recording.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
