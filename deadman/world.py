"""The production environment the agent acts on (the system of record).

`revert_pr` is deliberately NOT naturally idempotent — calling it twice does damage
twice. That is what makes the kill-mid-rollback the headline WOW: the naive agent
double-reverts; DEADMAN reverts exactly once.
"""
from __future__ import annotations


class World:
    def __init__(self):
        self.applied: list[tuple] = []   # every side effect that actually hit prod

    def revert_pr(self, pr: str, key: str | None = None):
        self.applied.append(("revert_pr", pr, key))

    def cordon_drain(self, node: str, key: str | None = None):
        self.applied.append(("cordon_drain", node, key))

    def asg_scale(self, asg: str, replicas: int, key: str | None = None):
        self.applied.append(("asg_scale", asg, replicas, key))

    # system-of-record queries DEADMAN uses to verify before re-acting
    def is_reverted(self, pr: str) -> bool:
        return any(a == "revert_pr" and p == pr for (a, p, *_rest) in self.applied)

    def count(self, action: str) -> int:
        return sum(1 for rec in self.applied if rec[0] == action)
