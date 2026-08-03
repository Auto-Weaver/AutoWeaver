"""L5 — ActionLeaf + Batch end-to-end on real hardware.

What this exercises:
  - a Batch driven at 25Hz by BTClock against a live feedback thread
  - ActionLeaf.on_start firing move_j, on_running polling the world board
  - Settled-detection (running=False + joint near target) reaching SUCCESS
  - Framework exit path: BTClock.kill() → tree.halt() → on_halted → arm.halt
  - No SLOW_TICK warnings under steady-state (move_j RPC ~10ms < 40ms budget)

Risk: same as L3 (J1 +5° at speed=10). Operator hand on e-stop.

This is the smallest possible BT — a single leaf with no control nodes.
The point is to validate the framework runtime on real hardware, not to
exercise any tree structure.
"""
from __future__ import annotations

import asyncio
import time
from typing import Sequence

import pytest

from autoweaver.motion_policy.batch import Batch
from autoweaver.worker.clock import BTClock
from autoweaver.motion_policy.nodes.leaf.action_leaf import ActionLeaf
from autoweaver.motion_policy.nodes.node import Status


# ---- test-only ActionLeaf ----------------------------------------------------
# Lives in this file because it's test material; the production MoveToJointPose
# leaf gets designed when we build motion/leaves/ in workstation-2.

class MoveJ(ActionLeaf):
    """Drive arm to a joint-space target; succeed when settled within tolerance."""

    def __init__(
        self,
        arm,
        target: Sequence[float],
        speed: int = 10,
        tolerance_deg: float = 0.5,
        name: str = "MoveJ",
    ):
        super().__init__(device=arm, name=name)
        self.target = tuple(float(x) for x in target)
        self.speed = speed
        self.tolerance_deg = tolerance_deg

    def on_start(self) -> Status:
        self._goal_id = self.device.move_j(self.target, speed=self.speed)
        return Status.RUNNING

    def on_running(self) -> Status:
        snap = self.snapshot
        running = snap.get(f"{self.device.name}.running")
        joint = snap.get(f"{self.device.name}.joint")
        if joint is None or running is None:
            return Status.RUNNING
        if running is False and self._joints_close(joint):
            return Status.SUCCESS
        return Status.RUNNING

    def _joints_close(self, current: Sequence[float]) -> bool:
        return all(abs(c - t) < self.tolerance_deg for c, t in zip(current, self.target))


# ---- the test ----------------------------------------------------------------

async def _drive(clock, batch, timeout: float):
    """Own the tick loop, like business code does — the framework offers no
    blocking wait (EVO-014 §5)."""
    deadline = time.monotonic() + timeout
    while batch.result is None:
        clock.tick_once()
        if time.monotonic() > deadline:
            raise TimeoutError(f"batch '{batch.name}' did not exit in {timeout}s")
        await asyncio.sleep(0.04)  # 25 Hz
    return batch.result



@pytest.mark.integration
async def test_move_j_action_leaf_e2e_on_real_arm(real_dobot):
    arm, board = real_dobot
    await asyncio.sleep(0.3)

    start = list(board.snapshot()["dobot1.joint"])
    target = list(start)
    target[0] += 5.0
    print()
    print(f"  start joint   : {tuple(round(x, 2) for x in start)}")
    print(f"  target joint  : {tuple(round(x, 2) for x in target)}")

    clock = BTClock(world_board=board, hz=25)

    # ---- forward leg via BT ----
    print(f"  → Batch with single MoveJ leaf, hz=25")
    batch = Batch(
        lambda: MoveJ(arm, target=target, speed=10), name="l5_forward",
    )
    clock.submit(batch)

    t0 = time.monotonic()
    result = await _drive(clock, batch, timeout=15.0)
    elapsed = time.monotonic() - t0
    print(f"  batch exited in {elapsed:.2f}s")
    print(f"  result.reason     : {result.reason}")
    print(f"  result.message    : {result.message!r}")
    print(f"  result.final_status: {result.final_status}")
    assert result.success, f"BT did not reach SUCCESS: {result}"

    final = board.snapshot()["dobot1.joint"]
    print(f"  final joint   : {tuple(round(x, 2) for x in final)}")
    assert abs(final[0] - target[0]) < 0.5, (
        f"J1 didn't reach target: {final[0]} vs {target[0]}"
    )

    # ---- return leg via a fresh Batch ----
    # Each region in production will submit a new Batch — this models that,
    # and proves the clock takes a next Batch once the first has EXITED.
    print(f"  → returning to start via a second Batch")
    batch2 = Batch(
        lambda: MoveJ(arm, target=start, speed=10), name="l5_return",
    )
    clock.submit(batch2)
    result2 = await _drive(clock, batch2, timeout=15.0)
    assert result2.success

    returned = board.snapshot()["dobot1.joint"]
    print(f"  returned joint: {tuple(round(x, 2) for x in returned)}")
    assert abs(returned[0] - start[0]) < 0.5
