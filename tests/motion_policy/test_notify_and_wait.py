"""Tests for NotifyAndWait + WaitForAdvance."""

from __future__ import annotations

import pytest

from autoweaver.motion_policy.blackboard import Blackboard
from autoweaver.motion_policy.nodes.leaf.notify_and_wait import (
    NotifyAndWait,
    WaitForAdvance,
)
from autoweaver.motion_policy.nodes.node import Status
from autoweaver.motion_policy.world_board import WorldBoard


# ─── NotifyAndWait ─────────────────────────────────────────────────────────


def _new_node_with_bb(leaf):
    """Inject a Blackboard so leaves that call self._blackboard work."""
    leaf.set_blackboard(Blackboard())
    return leaf


def test_notify_and_wait_passes_note_with_request_id():
    board = WorldBoard()
    received: list[dict] = []
    board.accept_notes("worker_a", "do_thing", dict, received.append)

    leaf = _new_node_with_bb(
        NotifyAndWait(
            world_board=board,
            target="worker_a",
            note_name="do_thing",
            payload={"x": 1},
        )
    )

    # First tick: dispatches the note, returns RUNNING (no completion yet).
    status = leaf.tick(board.snapshot())
    assert status == Status.RUNNING

    # Deliver to flush the queue.
    board.deliver_notes()
    assert len(received) == 1
    delivered = received[0]
    assert delivered["x"] == 1
    # request_id was auto-injected.
    assert "__request_id__" in delivered
    assert isinstance(delivered["__request_id__"], int)
    assert delivered["__request_id__"] > 0


def test_notify_and_wait_succeeds_when_last_completed_id_catches_up():
    board = WorldBoard()
    received: list[dict] = []
    board.accept_notes("worker_a", "do_thing", dict, received.append)
    board.declare_state("worker_a.last_completed_id", int, writer="worker_a")
    board.post_state("worker_a.last_completed_id", 0, writer="worker_a")

    leaf = _new_node_with_bb(
        NotifyAndWait(
            world_board=board,
            target="worker_a",
            note_name="do_thing",
        )
    )

    # Tick 1: dispatches, returns RUNNING.
    status = leaf.tick(board.snapshot())
    assert status == Status.RUNNING
    board.deliver_notes()
    rid = received[0]["__request_id__"]

    # Worker hasn't completed yet → still RUNNING on next tick.
    assert leaf.tick(board.snapshot()) == Status.RUNNING

    # Worker writes last_completed_id == rid → leaf flips to SUCCESS.
    board.post_state("worker_a.last_completed_id", rid, writer="worker_a")
    assert leaf.tick(board.snapshot()) == Status.SUCCESS


def test_two_leaves_in_sequence_each_wait_for_own_rid():
    """Race scenario: leaf 2 must NOT see leaf 1's completion as its
    own. Each leaf only succeeds when its own rid is reached."""
    board = WorldBoard()
    received: list[dict] = []
    board.accept_notes("worker_a", "do_thing", dict, received.append)
    board.declare_state("worker_a.last_completed_id", int, writer="worker_a")
    board.post_state("worker_a.last_completed_id", 0, writer="worker_a")

    leaf1 = _new_node_with_bb(
        NotifyAndWait(world_board=board, target="worker_a", note_name="do_thing")
    )
    leaf2 = _new_node_with_bb(
        NotifyAndWait(world_board=board, target="worker_a", note_name="do_thing")
    )

    # Leaf 1: dispatch + still running.
    leaf1.tick(board.snapshot())
    board.deliver_notes()
    rid1 = received[0]["__request_id__"]
    assert leaf1.tick(board.snapshot()) == Status.RUNNING

    # Worker completes leaf 1's request.
    board.post_state("worker_a.last_completed_id", rid1, writer="worker_a")
    assert leaf1.tick(board.snapshot()) == Status.SUCCESS

    # Leaf 2: dispatch. last_completed_id is already at rid1, but
    # leaf 2's rid is rid1+ ; leaf 2 must NOT see rid1's completion
    # as its own.
    status = leaf2.tick(board.snapshot())
    board.deliver_notes()
    rid2 = received[1]["__request_id__"]
    assert rid2 > rid1
    assert status == Status.RUNNING

    # Still RUNNING — last_completed_id stuck at rid1 < rid2.
    assert leaf2.tick(board.snapshot()) == Status.RUNNING

    # Worker completes leaf 2's request.
    board.post_state("worker_a.last_completed_id", rid2, writer="worker_a")
    assert leaf2.tick(board.snapshot()) == Status.SUCCESS


def test_notify_and_wait_payload_callable_1arg_receives_blackboard():
    board = WorldBoard()
    received: list[dict] = []
    board.accept_notes("worker_a", "do_thing", dict, received.append)

    bb = Blackboard()
    bb.set_initial("target_x", 42.0)

    leaf = NotifyAndWait(
        world_board=board,
        target="worker_a",
        note_name="do_thing",
        payload=lambda b: {"pose": (b.read("target_x"), 0, 0, 0, 0, 0)},
    )
    leaf.set_blackboard(bb)

    leaf.tick(board.snapshot())
    board.deliver_notes()
    assert received[0]["pose"] == (42.0, 0, 0, 0, 0, 0)


def test_notify_and_wait_payload_callable_2arg_receives_snapshot():
    board = WorldBoard()
    received: list[dict] = []
    board.accept_notes("worker_a", "do_thing", dict, received.append)
    board.declare_state("arm.pose", tuple, writer="arm")
    board.post_state("arm.pose", (1.0, 2.0, 3.0), writer="arm")

    leaf = NotifyAndWait(
        world_board=board,
        target="worker_a",
        note_name="do_thing",
        payload=lambda b, s: {"echoed_pose": s["arm.pose"]},
    )
    leaf.set_blackboard(Blackboard())

    leaf.tick(board.snapshot())
    board.deliver_notes()
    assert received[0]["echoed_pose"] == (1.0, 2.0, 3.0)


def test_notify_and_wait_reset_re_allocates_rid():
    """After SUCCESS the leaf can be re-ticked (e.g. inside a Repeat) and
    must allocate a fresh rid, not reuse the old one."""
    board = WorldBoard()
    received: list[dict] = []
    board.accept_notes("worker_a", "do_thing", dict, received.append)
    board.declare_state("worker_a.last_completed_id", int, writer="worker_a")
    board.post_state("worker_a.last_completed_id", 0, writer="worker_a")

    leaf = _new_node_with_bb(
        NotifyAndWait(world_board=board, target="worker_a", note_name="do_thing")
    )

    # Run 1
    leaf.tick(board.snapshot())
    board.deliver_notes()
    rid1 = received[0]["__request_id__"]
    board.post_state("worker_a.last_completed_id", rid1, writer="worker_a")
    assert leaf.tick(board.snapshot()) == Status.SUCCESS

    # reset() is called automatically by tick() once we returned SUCCESS.
    # Run 2 must allocate a fresh rid.
    leaf.tick(board.snapshot())
    board.deliver_notes()
    rid2 = received[1]["__request_id__"]
    assert rid2 > rid1


# ─── WaitForAdvance ────────────────────────────────────────────────────────


def test_wait_for_advance_succeeds_when_state_exceeds_threshold():
    board = WorldBoard()
    board.declare_state("preview.advance_count", int, writer="preview")
    board.post_state("preview.advance_count", 0, writer="preview")

    bb = Blackboard()
    leaf = WaitForAdvance(state_key="preview.advance_count", bb_key="scan.last_advance")
    leaf.set_blackboard(bb)

    # Initial: threshold = 0, current = 0, not strictly > → RUNNING.
    assert leaf.tick(board.snapshot()) == Status.RUNNING

    # Producer advances the counter.
    board.post_state("preview.advance_count", 1, writer="preview")
    assert leaf.tick(board.snapshot()) == Status.SUCCESS

    # Threshold is now 1 in the blackboard.
    assert bb.read("scan.last_advance") == 1


def test_wait_for_advance_uses_blackboard_threshold_for_subsequent_calls():
    """On a second run, the blackboard threshold reflects the prior count
    — leaf only succeeds when the state advances *past* that."""
    board = WorldBoard()
    board.declare_state("preview.advance_count", int, writer="preview")
    board.post_state("preview.advance_count", 5, writer="preview")

    bb = Blackboard()
    bb.set_initial("scan.last_advance", 5)  # already-seen threshold

    leaf = WaitForAdvance(state_key="preview.advance_count", bb_key="scan.last_advance")
    leaf.set_blackboard(bb)

    # state=5, threshold=5, not strictly > → RUNNING.
    assert leaf.tick(board.snapshot()) == Status.RUNNING

    board.post_state("preview.advance_count", 6, writer="preview")
    assert leaf.tick(board.snapshot()) == Status.SUCCESS
    assert bb.read("scan.last_advance") == 6
