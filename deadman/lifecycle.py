"""Incident lifecycle states, transition rules, and TTL helpers.

Pure data/logic — no I/O. Safe to import anywhere.

States
------
  TRIAGE      -> MITIGATING  -> RESOLVED -> CLOSED
  Any state   -> FAILED      (terminal; non-recoverable failure path)

Allowed transitions
-------------------
  TRIAGE      : MITIGATING, FAILED
  MITIGATING  : RESOLVED, TRIAGE (regression), FAILED
  RESOLVED    : CLOSED, MITIGATING (re-open)
  CLOSED      : (terminal — no further transitions allowed)
  FAILED      : (terminal — no further transitions allowed)
"""
from __future__ import annotations

import time

# ---------------------------------------------------------------------------
# State constants (use these everywhere — avoids raw string comparisons)
# ---------------------------------------------------------------------------

TRIAGE = "triage"
MITIGATING = "mitigating"
RESOLVED = "resolved"
CLOSED = "closed"
FAILED = "failed"

ALL_STATES: frozenset[str] = frozenset({TRIAGE, MITIGATING, RESOLVED, CLOSED, FAILED})

# Terminal states: no transitions out of these are permitted.
TERMINAL_STATES: frozenset[str] = frozenset({CLOSED, FAILED})

# Allowed forward/backward transition graph.
# Keys: current state. Values: set of states that current state may move to.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    TRIAGE: frozenset({MITIGATING, FAILED}),
    MITIGATING: frozenset({RESOLVED, TRIAGE, FAILED}),
    RESOLVED: frozenset({CLOSED, MITIGATING, FAILED}),
    CLOSED: frozenset(),  # terminal
    FAILED: frozenset(),  # terminal
}


# ---------------------------------------------------------------------------
# Transition validator
# ---------------------------------------------------------------------------


def validate_transition(old: str, new: str) -> None:
    """Raise ValueError if the transition old -> new is not permitted.

    Parameters
    ----------
    old:
        Current lifecycle state (must be a member of ALL_STATES).
    new:
        Proposed next lifecycle state (must be a member of ALL_STATES).

    Raises
    ------
    ValueError
        If either state is unknown, or if the transition is not in
        ALLOWED_TRANSITIONS[old].
    """
    if old not in ALL_STATES:
        raise ValueError(
            f"Unknown lifecycle state {old!r}. Valid states: {sorted(ALL_STATES)}"
        )
    if new not in ALL_STATES:
        raise ValueError(
            f"Unknown lifecycle state {new!r}. Valid states: {sorted(ALL_STATES)}"
        )
    if new not in ALLOWED_TRANSITIONS[old]:
        allowed = sorted(ALLOWED_TRANSITIONS[old])
        raise ValueError(
            f"Invalid lifecycle transition {old!r} -> {new!r}. "
            f"Allowed from {old!r}: {allowed}"
        )


# ---------------------------------------------------------------------------
# TTL helper (DynamoDB TTL attribute)
# ---------------------------------------------------------------------------


def ttl_epoch(retention_seconds: int = 2_592_000) -> int:
    """Return a Unix epoch (int) that is *retention_seconds* from now.

    Suitable for writing as the DynamoDB ``ttl`` attribute so DynamoDB's
    Time-To-Live feature automatically removes expired items.

    Parameters
    ----------
    retention_seconds:
        How many seconds from now before the item expires.
        Default: 2 592 000 s = 30 days.

    Returns
    -------
    int
        Unix epoch (seconds since 1970-01-01 UTC) of the expiry point.
    """
    if retention_seconds < 0:
        raise ValueError(f"retention_seconds must be >= 0, got {retention_seconds}")
    return int(time.time()) + retention_seconds
