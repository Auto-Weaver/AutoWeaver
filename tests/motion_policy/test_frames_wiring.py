"""Wiring test: Frames injected through BTClock → Action → tree → leaf.

Validates that a leaf can call ``self.lookup(target, source)`` and have it
resolve against the live WorldBoard snapshot, with the Frames graph injected
once at attach time (mirroring how the blackboard is injected).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import numpy as np
import pytest

from autoweaver.frames import Frames
from autoweaver.motion_policy.action import Action
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.clock import BTClock


_CELL = """
    frames:
      - name: arm_1_base
        parent: world
        xyz: [0, 0, 0]
        rpy: [0, 0, 0]
      - name: arm_1_flange
        parent: arm_1_base
        dynamic:
          state_key: arm_1.pose
          required: true
      - name: arm_1_tool_gripper
        parent: arm_1_flange
        xyz: [0, 0, 100]
        rpy: [0, 0, 0]
"""


def _frames(tmp_path: Path) -> Frames:
    p = tmp_path / "cell.yaml"
    p.write_text(textwrap.dedent(_CELL).lstrip())
    return Frames(p)


def _pose(x, y, z) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = [x, y, z]
    return m


class _LookupLeaf(TreeNode):
    """Captures the world position of the gripper via self.lookup."""

    def __init__(self):
        super().__init__(name="LookupLeaf")
        self.captured: np.ndarray | None = None

    def on_start(self) -> Status:
        return self.on_running()

    def on_running(self) -> Status:
        T = self.lookup("world", "arm_1_tool_gripper")
        self.captured = T[:3, 3]
        return Status.SUCCESS


def test_leaf_lookup_resolves_through_clock(tmp_path):
    board = WorldBoard()
    board.declare_state("arm_1.pose", np.ndarray, writer="arm_1")
    board.post_state("arm_1.pose", _pose(500, 0, 200), writer="arm_1")

    leaf = _LookupLeaf()
    action = Action(tree=leaf)
    clock = BTClock(world_board=board, frames=_frames(tmp_path))
    clock.attach_tree(action)

    clock.tick_once()
    # flange at (500,0,200); gripper +100z → world (500, 0, 300).
    assert leaf.captured is not None
    assert np.allclose(leaf.captured, [500, 0, 300])


def test_lookup_without_frames_raises(tmp_path):
    """No frames= on the clock → lookup() fails loud, not silently."""
    board = WorldBoard()
    leaf = _LookupLeaf()
    action = Action(tree=leaf)
    clock = BTClock(world_board=board)  # no frames
    clock.attach_tree(action)

    clock.tick_once()
    # The leaf's lookup raises RuntimeError; TreeNode.tick catches it and the
    # node goes FAILURE (the exception is recorded on the node).
    assert leaf.status in (Status.FAILURE, Status.IDLE)
    assert leaf._exception is not None
    assert "no Frames was injected" in str(leaf._exception)


def test_frames_injected_into_nested_tree(tmp_path):
    """set_frames must propagate through control nodes to a deep leaf."""
    board = WorldBoard()
    board.declare_state("arm_1.pose", np.ndarray, writer="arm_1")
    board.post_state("arm_1.pose", _pose(0, 0, 0), writer="arm_1")

    leaf = _LookupLeaf()
    # Wrap the leaf in a Sequence (a control node) to test propagation.
    tree = leaf >> _LookupLeaf()
    action = Action(tree=tree)
    clock = BTClock(world_board=board, frames=_frames(tmp_path))
    clock.attach_tree(action)

    clock.tick_once()
    # flange at origin; gripper +100z → (0,0,100).
    assert np.allclose(leaf.captured, [0, 0, 100])
