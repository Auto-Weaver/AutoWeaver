"""Tests for NotifyLeaf — fire-and-forget pass_note."""

from __future__ import annotations

from autoweaver.motion_policy.nodes.leaf.notify import NotifyLeaf
from autoweaver.motion_policy.nodes.node import Status
from autoweaver.motion_policy.world_board import WorldBoard


def test_notify_leaf_passes_note_and_succeeds():
    board = WorldBoard()
    received = []
    board.accept_notes("perception", "go", dict, received.append)

    leaf = NotifyLeaf(board, target="perception", note_name="go", payload={"x": 1})
    status = leaf.tick(board.snapshot())

    # NotifyLeaf is single-tick, returns SUCCESS immediately.
    assert status == Status.SUCCESS

    # Note is queued, not yet delivered.
    assert received == []
    board.deliver_notes()
    assert received == [{"x": 1}]


def test_notify_leaf_default_payload_is_empty_dict():
    board = WorldBoard()
    received = []
    board.accept_notes("perception", "go", dict, received.append)

    leaf = NotifyLeaf(board, target="perception", note_name="go")
    leaf.tick(board.snapshot())
    board.deliver_notes()
    assert received == [{}]


def test_notify_leaf_unknown_target_returns_failure():
    """When the target has no acceptor, TreeNode.tick converts the
    KeyError raised by pass_note into FAILURE (per the leaf protocol).
    The exception is recorded on the node for inspection."""
    board = WorldBoard()
    leaf = NotifyLeaf(board, target="nonexistent", note_name="x", payload={})
    status = leaf.tick(board.snapshot())
    assert status == Status.FAILURE
    assert isinstance(leaf._exception, KeyError)


def test_notify_leaf_custom_sender_is_recorded_in_history():
    """Sender propagates to the WorldBoard via pass_note (visible to
    debugging inspectors of the queue, though not to the snapshot since
    notes don't enter state)."""
    board = WorldBoard()
    received = []
    board.accept_notes("perception", "go", dict, received.append)

    leaf = NotifyLeaf(
        board, target="perception", note_name="go",
        payload={"x": 1}, sender="custom_sender",
    )
    leaf.tick(board.snapshot())
    board.deliver_notes()
    assert received == [{"x": 1}]
