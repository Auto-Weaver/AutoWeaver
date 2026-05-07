"""L5 — ActionLeaf + Action.run() end-to-end on real hardware.

What this exercises:
  - Action's 25Hz asyncio tick loop running against a live feedback thread
  - ActionLeaf.on_start firing move_j, on_running polling the world board
  - Settled-detection (running=False + joint near target) reaching SUCCESS
  - Framework halt path: tree.halt() in finally → on_halted → arm.halt
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

from autoweaver.motion_policy.action import Action
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

    # ---- forward leg via BT ----
    print(f"  → Action.run() with single MoveJ leaf, hz=25")
    leaf = MoveJ(arm, target=target, speed=10)
    action = Action(tree=leaf, world_board=board, hz=25, name="l5_forward")

    t0 = time.monotonic()
    result = await asyncio.wait_for(action.run(), timeout=15.0)
    elapsed = time.monotonic() - t0
    print(f"  Action.run() returned in {elapsed:.2f}s")
    print(f"  result.success    : {result.success}")
    print(f"  result.message    : {result.message!r}")
    print(f"  result.final_status: {result.final_status}")
    assert result.success, f"BT did not reach SUCCESS: {result}"

    final = board.snapshot()["dobot1.joint"]
    print(f"  final joint   : {tuple(round(x, 2) for x in final)}")
    assert abs(final[0] - target[0]) < 0.5, (
        f"J1 didn't reach target: {final[0]} vs {target[0]}"
    )

    # ---- return leg via a fresh Action ----
    # Each region in production will spawn a new Action — this models that.
    print(f"  → returning to start via second Action.run()")
    leaf2 = MoveJ(arm, target=start, speed=10)
    action2 = Action(tree=leaf2, world_board=board, hz=25, name="l5_return")
    result2 = await asyncio.wait_for(action2.run(), timeout=15.0)
    assert result2.success

    returned = board.snapshot()["dobot1.joint"]
    print(f"  returned joint: {tuple(round(x, 2) for x in returned)}")
    assert abs(returned[0] - start[0]) < 0.5
