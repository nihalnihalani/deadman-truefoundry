"""The split-screen chaos demo — naive agent vs DEADMAN, side by side.

    python scripts/run_demo.py

Injects the challenge's failures and prints the Resilience Scoreboard. The headline:
NAIVE double-executes a destructive rollback; DEADMAN does it exactly once.
"""
import os
# Force mock mode BEFORE importing deadman.* so this demo never needs credentials.
# config.py auto-loads .env via python-dotenv, which does NOT override an env var that
# is already set — so setting it here wins even when a repo-root .env says MODE=real.
os.environ["DEADMAN_MODE"] = "mock"
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deadman import state
from deadman.world import World
from deadman.chaos import Chaos
from deadman.commander import NaiveAgent, Deadman, action_key
from deadman.mcp_gateway import KillSignal

INCIDENT = "demo-incident-42"
REVERT_KEY = action_key(INCIDENT, "revert_pr", "PR-1337")


def run_naive():
    chaos = Chaos()
    chaos.correlated_blackout()   # us-east-1 EVENT
    chaos.kill_bedrock()
    return NaiveAgent(World()).run(chaos)


def run_deadman():
    state.reset(INCIDENT)
    world = World()
    chaos = Chaos()
    # Beat 1-3: correlated blackout + 429 storm + regional Bedrock outage -> deep fallback
    chaos.correlated_blackout()
    chaos.rate_limit_storm()
    # Beat 4: corrupt intermediate output (Post-Tool guardrail catches it)
    chaos.corrupt_output = True
    # Beat 5 (WOW): kill mid-rollback
    chaos.kill_process_after(REVERT_KEY)

    world_shared = world
    agent = Deadman(INCIDENT, world_shared, chaos)
    try:
        agent.run()
    except KillSignal:
        pass
    chaos.kill_after = None
    return Deadman(INCIDENT, world_shared, chaos).run(resume=True)


def bar(label, naive, dead):
    return f"  {label:<22} NAIVE: {str(naive):<14} DEADMAN: {dead}"


def main():
    print("=" * 70)
    print("  DEADMAN — the agent that survives its own outage  ·  CHAOS DEMO")
    print("=" * 70)
    print('\n  "The thing fighting the fire is standing in the fire."\n')

    naive = run_naive()
    dead = run_deadman()

    print("  RESILIENCE SCOREBOARD")
    print("  " + "-" * 60)
    print(bar("Backend in use", "us-east-1 (dead)", dead.backend))
    print(bar("Fallback depth", "—", dead.fallback_depth))
    print(bar("Guardrail blocks", 0, dead.guardrail_blocks))
    print(bar("Drain authority", "ON (ungoverned)", dead.drain_authority))
    print(bar("State losses", naive.state_losses, dead.state_losses))
    print(bar("Survived", naive.survived, dead.survived))
    print("  " + "-" * 60)
    print(bar(">> DOUBLE-EXECUTIONS", naive.double_executions, dead.double_executions))
    print("  " + "-" * 60)

    print("\n  DEADMAN timeline:")
    for n in dead.notes:
        print("   - " + n)

    print(f"\n  THE WHOLE PITCH, IN ONE NUMBER:")
    print(f"     Double-executions  ->  NAIVE: {naive.double_executions}   DEADMAN: {dead.double_executions}")
    print("\n  Run `python scripts/prove_exactly_once.py` for the asserted Day-1 proof.")


if __name__ == "__main__":
    main()
