"""WaitFor — block until a WorldBoard state field satisfies a predicate.

Reads ``ctx.snapshot`` each tick (the snapshot is injected by the BT
engine via ``TreeNode.tick``). Returns SUCCESS as soon as the predicate
holds; returns RUNNING otherwise. Pure read — no side effects.
"""

from __future__ import annotations

from typing import Any, Callable

from autoweaver.motion_policy.nodes.node import Status, TreeNode


class WaitFor(TreeNode):
    """Wait until a WorldBoard state key satisfies a predicate.

    ``key`` is the full state key path (e.g. ``"perception.state"``).
    The value is read from the per-tick snapshot, so all reads within
    a tick see a consistent view.

    Common patterns:

        # Wait for any non-None value:
        WaitFor("perception.next_target")

        # Wait for a specific equality:
        WaitFor("perception.state", lambda s: s == "picked")

        # Compose with timeout:
        WaitFor("vacuum.sealed").timeout(2.0)

    The leaf is stateless — every tick re-reads from the snapshot. This
    makes it easy to test and reason about.
    """

    def __init__(
        self,
        key: str,
        predicate: Callable[[Any], bool] | None = None,
        name: str = "",
    ):
        super().__init__(name=name or f"WaitFor({key})")
        self._key = key
        self._predicate = predicate or _default_predicate

    def on_start(self) -> Status:
        return self._evaluate()

    def on_running(self) -> Status:
        return self._evaluate()

    def _evaluate(self) -> Status:
        value = self.snapshot.get(self._key)
        return Status.SUCCESS if self._predicate(value) else Status.RUNNING


def _default_predicate(value: Any) -> bool:
    """Default: succeed as soon as the key has a non-None value."""
    return value is not None
