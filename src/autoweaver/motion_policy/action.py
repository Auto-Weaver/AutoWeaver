from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from autoweaver.motion_policy.blackboard import Blackboard
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.tracer import ActionTracer, NullTracer
from autoweaver.motion_policy.world_board import Snapshot

if TYPE_CHECKING:
    from autoweaver.worker.base import TickContext


logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    success: bool
    message: str = ""
    exception: BaseException | None = None
    failed_node: str | None = None
    final_status: Status = Status.IDLE


class Action:
    """Holds a BT tree, its Blackboard, and per-tick instrumentation.

    In 0.5.0 the ``Action`` is no longer a self-driving tick loop —
    that responsibility moved to ``BTClock`` (see EVO-007). The Action's
    role is:

      - own one BT tree and its Blackboard
      - expose ``tick(snapshot)`` for the clock to call once per tick
      - emit tracer events around each tick
      - propagate ``halt()`` to the tree when detached

    BTClock attaches the Action via ``BTClock.attach_tree(action)`` and
    calls ``action.tick(snapshot)`` every tick. The Action records its
    own pass/fail outcome (in ``last_result``) the first tick the root
    returns SUCCESS or FAILURE; subsequent ticks are no-ops.
    """

    SLOW_TICK_DEFAULT_BUDGET_S = 0.04  # 40 ms — generous default at 25 Hz

    def __init__(
        self,
        tree: TreeNode,
        name: str = "",
        tracer: ActionTracer | None = None,
        slow_tick_budget_s: float = SLOW_TICK_DEFAULT_BUDGET_S,
    ):
        self.tree = tree
        self.name = name or tree.name
        self._tracer: ActionTracer = tracer if tracer is not None else NullTracer()
        self._slow_tick_budget_s = slow_tick_budget_s

        self._tick_seq = 0
        self._started = False
        self._finished = False
        self.last_result: ActionResult | None = None

        self.tree.set_blackboard(Blackboard())

    def set_frames(self, frames) -> None:
        """Inject the cell's coordinate-frame graph into the whole tree.

        Called by BTClock at attach time when the clock was given a
        ``frames=``. Propagates to every node via ``TreeNode.set_frames``.
        """
        self.tree.set_frames(frames)

    def tick(self, snapshot: Snapshot, ctx: TickContext | None = None) -> Status:
        """Run one tree tick. Called by BTClock.

        ``ctx`` is the tick's ``TickContext``; it is handed to the tree so
        every node sees one consistent tick time (``self.now``). It is
        optional so a bare ``action.tick(snapshot)`` still works, but nodes
        that read ``self.now`` will raise without it.

        After the tree first returns a terminal status (SUCCESS/FAILURE),
        further calls are no-ops and return that status without touching
        the tree.
        """
        if self._finished:
            return self.last_result.final_status if self.last_result else Status.IDLE

        if not self._started:
            self._tracer.on_action_start(self.name)
            self._started = True

        self._tick_seq += 1
        self._tracer.on_tick_start(self._tick_seq)
        t0 = time.monotonic()

        status = self.tree.tick(snapshot, ctx)

        duration = time.monotonic() - t0
        self._tracer.on_tick_end(self._tick_seq, duration, status)

        if duration > self._slow_tick_budget_s:
            logger.warning(
                "slow tick in action '%s': %.1fms (budget %.1fms)",
                self.name,
                duration * 1000,
                self._slow_tick_budget_s * 1000,
            )
            self._tracer.on_slow_tick(duration, self._slow_tick_budget_s)

        if status == Status.SUCCESS:
            self.last_result = ActionResult(success=True, final_status=status)
            self._tracer.on_action_end(self.name, self.last_result)
            self._finished = True
        elif status == Status.FAILURE:
            self.last_result = self._build_failure_result(status)
            self._tracer.on_action_end(self.name, self.last_result)
            self._finished = True

        return status

    def halt(self) -> None:
        """Halt the tree. BTClock calls this on detach.

        Idempotent. Safe to call from threads other than the clock's
        tick thread, since ``tree.halt()`` only walks the tree and
        invokes node-local cleanup.
        """
        if self._finished:
            return
        try:
            self.tree.halt()
        finally:
            if self._started and self.last_result is None:
                self.last_result = ActionResult(
                    success=False,
                    message="halted",
                    final_status=self.tree.status,
                )
                self._tracer.on_action_end(self.name, self.last_result)
            self._finished = True

    def _build_failure_result(self, final_status: Status) -> ActionResult:
        exc, failed = self._collect_failure_info(self.tree)
        if exc is not None:
            self._tracer.on_node_exception(failed or "<unknown>", exc)
        return ActionResult(
            success=False,
            message="Tree returned FAILURE" if exc is None else f"node '{failed}' raised",
            exception=exc,
            failed_node=failed,
            final_status=final_status,
        )

    @staticmethod
    def _collect_failure_info(
        node: TreeNode,
    ) -> tuple[BaseException | None, str | None]:
        """Walk the tree and return the first node carrying an exception."""
        if node._exception is not None:
            return node._exception, node.name
        for child in node._children_list():
            if child is node:
                continue
            exc, name = Action._collect_failure_info(child)
            if exc is not None:
                return exc, name
        return None, None
