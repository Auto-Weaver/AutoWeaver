"""End-to-end test: ActionLeaf + MockArm + BTClock-driven tick.

Validates the contract between the BT decision layer and a device:
  - Goals flow ActionLeaf -> arm.move_j
  - Pose is observed via WorldBoard snapshot
  - SUCCESS is reached when the leaf decides the move completed
  - halt propagates from the Action down to arm.halt
"""

from __future__ import annotations

import math
import time
from typing import Sequence

from autoweaver.device.arm.mock import MockArm
from autoweaver.motion_policy.action import Action
from autoweaver.motion_policy.nodes.leaf.action_leaf import ActionLeaf
from autoweaver.motion_policy.nodes.node import Status
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.subsystem.clock import BTClock


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


def test_action_leaf_drives_mock_arm_to_success():
    arm = MockArm(name="m1", feedback_hz=200, move_duration=0.0)
    board = WorldBoard()
    arm.register_outputs(board)
    arm.start()
    try:
        leaf = MoveJ(arm, target=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
        action = Action(tree=leaf)
        clock = BTClock(world_board=board, hz=100)
        clock.attach_tree(action)
        try:
            # Drive ticks until the tree finishes; bound it so a stuck
            # leaf doesn't deadlock the test. MockArm's feedback thread
            # runs at 200 Hz, so a small sleep between ticks lets it
            # publish the new joint values.
            for _ in range(200):
                clock.tick_once()
                if action.last_result is not None:
                    break
                time.sleep(0.01)
        finally:
            clock.shutdown()
    finally:
        arm.stop()

    assert action.last_result is not None
    assert action.last_result.success is True
    move_j_calls = [c for c in arm.calls if c[0] == "move_j"]
    assert len(move_j_calls) == 1
    assert board.snapshot()["m1.joint"] == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)


def test_action_halt_propagates_to_arm():
    """If the tree is detached mid-flight, arm.halt must be called."""
    arm = MockArm(name="m1", feedback_hz=200, move_duration=10.0)
    board = WorldBoard()
    arm.register_outputs(board)
    arm.start()
    try:
        leaf = MoveJ(arm, target=(9.0, 9.0, 9.0, 0.0, 0.0, 0.0))
        action = Action(tree=leaf)
        clock = BTClock(world_board=board, hz=100)
        handle = clock.attach_tree(action)
        try:
            # Tick a few times so the leaf actually issues the goal.
            for _ in range(3):
                clock.tick_once()
                time.sleep(0.005)
            # Now detach — the tree should be halted.
            clock.detach_tree(handle)
            action.halt()  # idempotent — completes the result if not already
        finally:
            clock.shutdown()
    finally:
        arm.stop()

    assert action.last_result is not None
    assert action.last_result.success is False
    assert action.last_result.message == "halted"
    halt_calls = [c for c in arm.calls if c[0] == "halt"]
    assert len(halt_calls) == 1
    halted_gid = halt_calls[0][1]
    move_calls = [c for c in arm.calls if c[0] == "move_j"]
    assert halted_gid == move_calls[0][1]
