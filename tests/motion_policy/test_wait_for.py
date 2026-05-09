"""Tests for WaitFor — block until WorldBoard state satisfies predicate."""

from __future__ import annotations

from autoweaver.motion_policy.nodes.leaf.wait_for import WaitFor
from autoweaver.motion_policy.nodes.node import Status
from autoweaver.motion_policy.world_board import WorldBoard


def test_wait_for_running_when_key_absent():
    board = WorldBoard()
    leaf = WaitFor("perception.state")
    assert leaf.tick(board.snapshot()) == Status.RUNNING


def test_wait_for_succeeds_when_key_set_with_default_predicate():
    """Default predicate: key has a non-None value."""
    board = WorldBoard()
    board.declare_state("perception.state", str, writer="perception")
    board.post_state("perception.state", "ready", writer="perception")

    leaf = WaitFor("perception.state")
    assert leaf.tick(board.snapshot()) == Status.SUCCESS


def test_wait_for_with_custom_predicate():
    board = WorldBoard()
    board.declare_state("perception.state", str, writer="perception")
    board.post_state("perception.state", "scanning", writer="perception")

    leaf = WaitFor("perception.state", lambda s: s == "picked")
    # Predicate not satisfied yet — RUNNING.
    assert leaf.tick(board.snapshot()) == Status.RUNNING

    board.post_state("perception.state", "picked", writer="perception")
    # Need a fresh snapshot to see the update.
    leaf.reset()  # so next tick goes through on_start path
    assert leaf.tick(board.snapshot()) == Status.SUCCESS


def test_wait_for_re_evaluates_each_tick():
    """If the value flips between ticks, WaitFor reflects the change."""
    board = WorldBoard()
    board.declare_state("ns.flag", bool, writer="ns")
    board.post_state("ns.flag", False, writer="ns")

    leaf = WaitFor("ns.flag", lambda v: v is True)
    # Tick 1: False → RUNNING
    assert leaf.tick(board.snapshot()) == Status.RUNNING
    # Tick 2 (RUNNING re-tick): still False
    assert leaf.tick(board.snapshot()) == Status.RUNNING

    board.post_state("ns.flag", True, writer="ns")
    # Tick 3: True → SUCCESS
    assert leaf.tick(board.snapshot()) == Status.SUCCESS


def test_wait_for_uses_the_per_tick_snapshot():
    """The snapshot passed to tick is what WaitFor reads, not the live board.

    This means a tick that captures snapshot N still sees the values as of
    seq N even if the board has been updated between snapshot capture and
    the tick.
    """
    board = WorldBoard()
    board.declare_state("ns.value", int, writer="ns")
    board.post_state("ns.value", 1, writer="ns")
    snap = board.snapshot()

    # Update after snapshot is captured.
    board.post_state("ns.value", 99, writer="ns")

    leaf = WaitFor("ns.value", lambda v: v == 99)
    # The leaf sees the snapshot's stale value (1), not the live 99.
    assert leaf.tick(snap) == Status.RUNNING
