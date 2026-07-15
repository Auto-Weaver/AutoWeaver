"""RepeatUntil — condition-driven do-while decorator.

Ticks the child; when the child SUCCEEDs, evaluates a snapshot predicate.
Predicate true → SUCCESS; false → RUNNING and the child re-runs next tick
(via the base-class auto-reset contract). At most one child pass completes
per tick — this is the framework-level guard against single-tick infinite
loops, replacing application-level "loop N times" fuses.

See EVO-010.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from autoweaver.motion_policy.nodes.decorator.base import DecoratorNode
from autoweaver.motion_policy.nodes.node import Status, TreeNode

if TYPE_CHECKING:
    from autoweaver.motion_policy.world_board import Snapshot


class RepeatUntil(DecoratorNode):
    """Run ``child`` repeatedly until ``cond(snapshot)`` holds (do-while).

    Semantics:

    - Tick the child.
    - child FAILURE → FAILURE.
    - child RUNNING → RUNNING (``cond`` is not evaluated).
    - child SUCCESS → evaluate ``cond(self.snapshot)``: True → SUCCESS,
      False → RUNNING (child re-runs next tick via its own auto-reset).

    ``cond`` is read-only over the snapshot; it is never handed the
    blackboard, so it cannot write. A ``cond`` that raises propagates to
    the base ``tick`` exception guard (node.py) and becomes FAILURE — no
    extra wrapping here.
    """

    def __init__(
        self,
        cond: Callable[[Snapshot], bool],
        child: TreeNode,
        name: str = "",
    ):
        super().__init__(child=child, name=name or "RepeatUntil")
        self._cond = cond

    def on_start(self) -> Status:
        return self._run()

    def on_running(self) -> Status:
        return self._run()

    def _run(self) -> Status:
        status = self.child.tick(self._snapshot)
        if status == Status.FAILURE:
            return Status.FAILURE
        if status == Status.RUNNING:
            return Status.RUNNING
        # child SUCCESS — one pass complete, test the exit condition.
        if self._cond(self.snapshot):
            return Status.SUCCESS
        return Status.RUNNING
