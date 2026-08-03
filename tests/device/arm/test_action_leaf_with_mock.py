"""End-to-end test: ActionLeaf + MockArm + BTClock-driven tick.

Validates the contract between the BT decision layer and a device:
  - Goals flow ActionLeaf -> arm.move_l
  - Pose is observed by pulling arm.get_flange_pose() (NEXT-008)
  - SUCCESS is reached when the leaf decides the move completed
  - halt propagates from the Batch down to arm.halt
"""

from __future__ import annotations

import time
from typing import Sequence

import numpy as np

from autoweaver.device.arm.mock import MockArm
from autoweaver.motion_policy.batch import Batch, ExitReason
from autoweaver.motion_policy.nodes.leaf.action_leaf import ActionLeaf
from autoweaver.motion_policy.nodes.node import Status
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.clock import BTClock


class MoveL(ActionLeaf):
    """Issue a Cartesian move and succeed when the flange reaches the target."""

    def __init__(self, arm, target_xyz: Sequence[float], name: str = "MoveL"):
        super().__init__(arm, name=name)
        self.arm = arm
        # target is just an xyz position; we ignore rotation for the test.
        self.target_xyz = tuple(float(x) for x in target_xyz)

    def on_start(self) -> Status:
        # Use (x, y, z, 0, 0, 0) for the 6-DOF target.
        self._goal_id = self.arm.move_l((*self.target_xyz, 0.0, 0.0, 0.0))
        return Status.RUNNING

    def on_running(self) -> Status:
        pose = self.arm.get_flange_pose()
        if np.allclose(pose[:3, 3], self.target_xyz, atol=1e-6):
            return Status.SUCCESS
        return Status.RUNNING


def test_action_leaf_drives_mock_arm_to_success():
    arm = MockArm(name="m1", move_duration=0.0)
    board = WorldBoard()
    arm.start()
    try:
        leaf = MoveL(arm, target_xyz=(10.0, 20.0, 30.0))
        batch = Batch(lambda: leaf)
        clock = BTClock(world_board=board, hz=100)
        clock.submit(batch)
        try:
            # Drive ticks until the batch finishes; bound it so a stuck
            # leaf doesn't deadlock the test.
            for _ in range(200):
                clock.tick_once()
                if batch.result is not None:
                    break
                time.sleep(0.005)
        finally:
            clock.shutdown()
    finally:
        arm.stop()

    assert batch.result is not None
    assert batch.result.success is True
    move_calls = [c for c in arm.calls if c[0] == "move_l"]
    assert len(move_calls) == 1


def test_action_halt_propagates_to_arm():
    """Killing a Batch mid-flight must reach arm.halt.

    This is EVO-010 known issue #3: ``detach_tree`` used to halt the tree
    but never the Action, so the Action-level teardown (the "halted"
    result, the tracer's end event) simply did not happen. ``kill`` is
    one exit path and does the whole thing — no follow-up call needed.
    """
    arm = MockArm(name="m1", move_duration=10.0)
    board = WorldBoard()
    arm.start()
    try:
        leaf = MoveL(arm, target_xyz=(9.0, 9.0, 9.0))
        batch = Batch(lambda: leaf)
        clock = BTClock(world_board=board, hz=100)
        handle = clock.submit(batch)
        try:
            # Tick a few times so the leaf actually issues the goal.
            for _ in range(3):
                clock.tick_once()
                time.sleep(0.005)
            clock.kill(handle)
        finally:
            clock.shutdown()
    finally:
        arm.stop()

    assert batch.result is not None
    assert batch.result.success is False
    assert batch.result.reason is ExitReason.KILLED
    assert batch.result.message == "killed"
    halt_calls = [c for c in arm.calls if c[0] == "halt"]
    assert len(halt_calls) == 1
    halted_gid = halt_calls[0][1]
    move_calls = [c for c in arm.calls if c[0] == "move_l"]
    assert halted_gid == move_calls[0][1]
