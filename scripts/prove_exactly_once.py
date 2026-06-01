"""Day-1 crown jewel: kill DEADMAN mid-rollback, resume in a FRESH agent, assert exactly-once.

    python scripts/prove_exactly_once.py

This is the de-risk gate: if this holds, the project's spine is proven. It kills the
commander between the side effect and the COMMIT, then rebuilds the agent from the
durable state + audit log (as a fresh process would) and proves the rollback ran ONCE.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deadman import state
from deadman.world import World
from deadman.chaos import Chaos
from deadman.commander import Deadman, REVERT_KEY
from deadman.mcp_gateway import KillSignal

INCIDENT = "prove-exactly-once"


def main():
    state.reset(INCIDENT)
    world = World()                       # shared system-of-record survives the "crash"
    chaos = Chaos()
    chaos.kill_process_after(REVERT_KEY)  # SIGKILL right after the revert side effect, before COMMIT

    print("=" * 66)
    print("  DEADMAN — proving exactly-once across a kill mid-rollback")
    print("=" * 66)

    # 1) run until the kill
    agent = Deadman(INCIDENT, world, chaos)
    try:
        agent.run()
        print("ERROR: expected a kill but the run completed")
        sys.exit(1)
    except KillSignal:
        print(f"[chaos ] SIGKILL after revert side effect; revert_pr applied so far: {world.count('revert_pr')}")

    # 2) a FRESH process resumes — only durable state + audit log survive
    chaos.kill_after = None
    resumed = Deadman(INCIDENT, world, chaos)   # rehydrates from disk
    sb = resumed.run(resume=True)

    print("[resume] notes:")
    for n in sb.notes:
        print("         - " + n)

    total = world.count("revert_pr")
    print(f"\n[assert] revert_pr applied to prod exactly {total} time(s)")
    assert total == 1, f"EXACTLY-ONCE VIOLATED: revert ran {total} times"
    print("[PASS  ] exactly-once across process death ✓  — the spine holds. Ship the rest.")


if __name__ == "__main__":
    main()
