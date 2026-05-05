"""End-to-end test: real ActionLeaf + MockArm + Action.run() loop.

This validates the contract between the BT decision layer and a device:
  - Goals flow ActionLeaf -> arm.move_j
  - Pose is observed via WorldBoard snapshot
  - SUCCESS is reached when the leaf decides the move completed
  - halt propagates from the Action down to arm.halt
"""

from __future__ import annotations

import asyncio
import math
from typing import Sequence

import pytest

from autoweaver.device.arm.mock import MockArm
from autoweaver.motion_policy.action import Action
from autoweaver.motion_policy.nodes.leaf.action_leaf import ActionLeaf
from autoweaver.motion_policy.nodes.node import Status
from autoweaver.motion_policy.world_board import WorldBoard


def _close(a: Sequence[float], b: Sequence[float], tol: float = 1e-6) -> bool:
    return all(math.isclose(x, y, abs_tol=tol) for x, y in zip(a, b))


class MoveJ(ActionLeaf):
    def __init__(self, arm, target: Sequence[float], name: str = "MoveJ"):
        super().__init__(arm, name=name)
        self.arm = arm
        self.target = tuple(float(x) for x in target)

    def on_start(self) -> Status:
        self._goal_id = self.arm.move_j(self.target)
        return Status.RUNNING

    def on_running(self) -> Status:
        joint = self.snapshot.get(f"{self.arm.name}.joint")
        if joint is not None and _close(joint, self.target):
            return Status.SUCCESS
        return Status.RUNNING


@pytest.mark.asyncio
async def test_action_leaf_drives_mock_arm_to_success():
    arm = MockArm(name="m1", feedback_hz=200, move_duration=0.0)
    board = WorldBoard()
    arm.register_outputs(board)
    arm.start()
    try:
        leaf = MoveJ(arm, target=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
        action = Action(tree=leaf, world_board=board, hz=100)
        result = await asyncio.wait_for(action.run(), timeout=2.0)
    finally:
        arm.stop()

    assert result.success is True
    move_j_calls = [c for c in arm.calls if c[0] == "move_j"]
    assert len(move_j_calls) == 1
    assert board.snapshot()["m1.joint"] == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)


@pytest.mark.asyncio
async def test_action_halt_propagates_to_arm():
    """If the Action is halted mid-flight, arm.halt must be called."""
    arm = MockArm(name="m1", feedback_hz=200, move_duration=10.0)
    board = WorldBoard()
    arm.register_outputs(board)
    arm.start()
    try:
        leaf = MoveJ(arm, target=(9.0, 9.0, 9.0, 0.0, 0.0, 0.0))
        action = Action(tree=leaf, world_board=board, hz=100)

        async def halt_soon():
            await asyncio.sleep(0.05)
            action.halt()

        halter = asyncio.create_task(halt_soon())
        result = await asyncio.wait_for(action.run(), timeout=2.0)
        await halter
    finally:
        arm.stop()

    assert result.success is False
    assert result.message == "halted"
    halt_calls = [c for c in arm.calls if c[0] == "halt"]
    assert len(halt_calls) == 1
    halted_gid = halt_calls[0][1]
    move_calls = [c for c in arm.calls if c[0] == "move_j"]
    assert halted_gid == move_calls[0][1]
