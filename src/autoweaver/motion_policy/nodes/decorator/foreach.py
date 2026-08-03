"""ForEach — data-driven iteration decorator.

Writes each item of a static sequence into a blackboard key in turn and
lets the child run to a terminal status once per item. One item advances
per tick (the child re-runs on the next tick via the base-class auto-reset
contract, see node.py tick()).

See EVO-010.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from autoweaver.motion_policy.nodes.decorator.base import DecoratorNode
from autoweaver.motion_policy.nodes.node import Status, TreeNode

if TYPE_CHECKING:
    from typing import Any


class ForEach(DecoratorNode):
    """Iterate ``child`` over ``items``, exposing the current item on the
    blackboard under ``key``.

    Semantics:

    - ``on_start``: idx=0. Empty ``items`` → immediate SUCCESS. Otherwise
      write ``items[0]`` into ``key`` and tick the child.
    - child SUCCESS → advance idx. If that was the last item → SUCCESS
      (terminal; the base-class auto-reset then clears idx). Otherwise
      write the next item and return RUNNING (at most one item per tick;
      the child re-runs next tick via its own auto-reset).
    - child FAILURE → FAILURE (aborts the whole traversal).
    - child RUNNING → RUNNING.

    The loop variable ``key`` is owned by the fixed writer ``"foreach"``:
    any ForEach may write any foreach key (idempotent same-writer
    registration), but a non-foreach writer touching such a key is
    rejected by the blackboard. Items may be any type (tuple/dict/…), so
    the key is registered untyped (``object``).
    """

    WRITER = "foreach"

    def __init__(
        self,
        key: str,
        items: Sequence[Any],
        child: TreeNode,
        name: str = "",
    ):
        super().__init__(child=child, name=name or f"ForEach({key})")
        self._key = key
        self._items = items
        self._idx = 0

    def set_blackboard(self, blackboard, key_mapping=None) -> None:
        super().set_blackboard(blackboard, key_mapping)
        blackboard.register_key(self._key, object, self.WRITER)

    def on_start(self) -> Status:
        self._idx = 0
        if len(self._items) == 0:
            return Status.SUCCESS
        self._write_current()
        return self._handle(self.child.tick(self._snapshot, self._tick_ctx))

    def on_running(self) -> Status:
        return self._handle(self.child.tick(self._snapshot, self._tick_ctx))

    def _handle(self, status: Status) -> Status:
        if status == Status.FAILURE:
            return Status.FAILURE
        if status == Status.RUNNING:
            return Status.RUNNING
        # child SUCCESS — advance one item.
        self._idx += 1
        if self._idx >= len(self._items):
            return Status.SUCCESS
        self._write_current()
        return Status.RUNNING

    def _write_current(self) -> None:
        self._blackboard.write(self._key, self._items[self._idx], self.WRITER)

    def reset(self) -> None:
        self._idx = 0
        super().reset()
