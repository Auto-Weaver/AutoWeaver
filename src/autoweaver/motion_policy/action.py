from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from autoweaver.motion_policy.blackboard import Blackboard
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.tracer import ActionTracer, NullTracer
from autoweaver.motion_policy.world_board import Snapshot, WorldBoard


logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    success: bool
    message: str = ""
    exception: BaseException | None = None
    failed_node: str | None = None
    final_status: Status = Status.IDLE


class Action:
    """Drives a BT tree with a fixed-frequency tick loop.

    One Action holds one tree, one Blackboard (BT working memory), and a
    reference to a shared WorldBoard (process-wide observable state).

    Each tick:
      1. Snapshot the WorldBoard (immutable, consistent across the tree)
      2. Tick the tree with that snapshot
      3. Sleep to maintain target frequency

    Halt:
      - External code calls action.halt() → sets _halted flag
      - The flag is checked at tick boundary (not mid-tick)
      - finally block guarantees tree.halt() is invoked on exit, propagating
        halt to all RUNNING subtrees so devices receive their halt calls
    """

    SLOW_TICK_MULTIPLIER = 2.0

    def __init__(
        self,
        tree: TreeNode,
        world_board: WorldBoard | None = None,
        hz: int = 25,
        name: str = "",
        tracer: ActionTracer | None = None,
    ):
        self.tree = tree
        self.interval = 1.0 / hz
        self.name = name or tree.name
        self.world_board = world_board if world_board is not None else WorldBoard()
        self._tracer: ActionTracer = tracer if tracer is not None else NullTracer()

        self._halted = False
        self._tick_seq = 0

        self.tree.set_blackboard(Blackboard())

    async def run(self) -> ActionResult:
        """Tick loop until tree completes, fails, or external halt."""
        self._tracer.on_action_start(self.name)
        result: ActionResult
        try:
            while not self._halted:
                t0 = time.monotonic()
                self._tick_seq += 1
                self._tracer.on_tick_start(self._tick_seq)

                snapshot = self.world_board.snapshot()
                status = self.tree.tick(snapshot)

                tick_duration = time.monotonic() - t0
                self._tracer.on_tick_end(self._tick_seq, tick_duration, status)

                if tick_duration > self.interval * self.SLOW_TICK_MULTIPLIER:
                    logger.warning(
                        "slow tick in action '%s': %.1fms (target %.1fms)",
                        self.name,
                        tick_duration * 1000,
                        self.interval * 1000,
                    )
                    self._tracer.on_slow_tick(tick_duration, self.interval)

                if status == Status.SUCCESS:
                    result = ActionResult(success=True, final_status=status)
                    return result
                if status == Status.FAILURE:
                    result = self._build_failure_result(status)
                    return result

                await asyncio.sleep(max(0.0, self.interval - tick_duration))

            result = ActionResult(
                success=False,
                message="halted",
                final_status=self.tree.status,
            )
            return result
        finally:
            self.tree.halt()
            self._tracer.on_action_end(self.name, result)

    def halt(self) -> None:
        """Request the tick loop to exit at the next tick boundary."""
        self._halted = True

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
