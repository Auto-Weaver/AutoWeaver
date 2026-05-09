from __future__ import annotations

import logging

import pytest

from autoweaver.motion_policy.action import Action, ActionResult
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.tracer import LogTracer
from autoweaver.motion_policy.world_board import WorldBoard


# ---- Test trees ---------------------------------------------------------

class _ImmediateSuccess(TreeNode):
    def on_start(self) -> Status:
        return Status.SUCCESS

    def on_running(self) -> Status:
        return Status.SUCCESS


class _ImmediateFailure(TreeNode):
    def on_start(self) -> Status:
        return Status.FAILURE

    def on_running(self) -> Status:
        return Status.FAILURE


class _BoomLeaf(TreeNode):
    def on_start(self) -> Status:
        raise ValueError("kaboom")

    def on_running(self) -> Status:
        return Status.RUNNING


class _NeverFinish(TreeNode):
    """Records every tick. Stays RUNNING forever unless halted."""

    def __init__(self):
        super().__init__()
        self.tick_count = 0
        self.snapshots_seen: list = []
        self.halted = False

    def on_start(self) -> Status:
        self.tick_count += 1
        self.snapshots_seen.append(self.snapshot)
        return Status.RUNNING

    def on_running(self) -> Status:
        self.tick_count += 1
        self.snapshots_seen.append(self.snapshot)
        return Status.RUNNING

    def on_halted(self) -> None:
        self.halted = True


class _RecordingTracer:
    def __init__(self):
        self.events: list[tuple] = []

    def on_action_start(self, action_name):
        self.events.append(("start", action_name))

    def on_action_end(self, action_name, result):
        self.events.append(("end", action_name, result.success))

    def on_tick_start(self, tick_seq):
        self.events.append(("tick_start", tick_seq))

    def on_tick_end(self, tick_seq, duration, root_status):
        self.events.append(("tick_end", tick_seq, root_status))

    def on_slow_tick(self, duration, target):
        self.events.append(("slow_tick", duration, target))

    def on_node_exception(self, node_name, exception):
        self.events.append(("node_exception", node_name, type(exception).__name__))


# ---- Helpers ------------------------------------------------------------

def _empty_snapshot() -> "Snapshot":
    return WorldBoard().snapshot()


# ---- Tests --------------------------------------------------------------

def test_tick_returns_success_on_root_success():
    action = Action(tree=_ImmediateSuccess())
    assert action.tick(_empty_snapshot()) == Status.SUCCESS
    assert action.last_result is not None
    assert action.last_result.success is True
    assert action.last_result.final_status == Status.SUCCESS


def test_tick_returns_failure_on_root_failure():
    action = Action(tree=_ImmediateFailure())
    assert action.tick(_empty_snapshot()) == Status.FAILURE
    assert action.last_result is not None
    assert action.last_result.success is False


def test_node_exception_recorded_in_action_result():
    leaf = _BoomLeaf()
    action = Action(tree=leaf)
    action.tick(_empty_snapshot())
    assert action.last_result is not None
    assert action.last_result.success is False
    assert isinstance(action.last_result.exception, ValueError)
    assert action.last_result.failed_node == leaf.name


def test_terminal_status_is_idempotent_on_subsequent_ticks():
    """Once SUCCESS/FAILURE is returned, further ticks don't re-tick the tree."""
    counter = {"count": 0}

    class _CountingSuccess(TreeNode):
        def on_start(self) -> Status:
            counter["count"] += 1
            return Status.SUCCESS

        def on_running(self) -> Status:
            counter["count"] += 1
            return Status.SUCCESS

    action = Action(tree=_CountingSuccess())
    assert action.tick(_empty_snapshot()) == Status.SUCCESS
    assert action.tick(_empty_snapshot()) == Status.SUCCESS
    assert action.tick(_empty_snapshot()) == Status.SUCCESS
    # Tree was only ticked once.
    assert counter["count"] == 1


def test_halt_propagates_to_tree_when_running():
    tree = _NeverFinish()
    action = Action(tree=tree)

    action.tick(_empty_snapshot())
    action.tick(_empty_snapshot())
    assert tree.tick_count == 2

    action.halt()
    assert tree.halted
    assert action.last_result is not None
    assert action.last_result.success is False
    assert action.last_result.message == "halted"

    # Subsequent ticks are no-ops.
    action.tick(_empty_snapshot())
    assert tree.tick_count == 2  # didn't advance


def test_halt_is_idempotent():
    tree = _NeverFinish()
    action = Action(tree=tree)
    action.tick(_empty_snapshot())
    action.halt()
    action.halt()  # should not raise


def test_halt_before_first_tick_is_safe():
    tree = _NeverFinish()
    action = Action(tree=tree)
    action.halt()  # Never started — should not call tracer.on_action_end
    assert action.last_result is None  # No started ⇒ no result


def test_snapshot_passed_to_tree_each_tick():
    board = WorldBoard()
    board.declare_state("test.k", int, writer="w")
    board.post_state("test.k", 1, writer="w")

    tree = _NeverFinish()
    action = Action(tree=tree)

    for _ in range(3):
        action.tick(board.snapshot())

    assert tree.tick_count == 3
    assert all(s["test.k"] == 1 for s in tree.snapshots_seen)


def test_tracer_lifecycle_events_on_success():
    tracer = _RecordingTracer()
    action = Action(tree=_ImmediateSuccess(), tracer=tracer)
    action.tick(_empty_snapshot())
    kinds = [e[0] for e in tracer.events]
    assert kinds[0] == "start"
    assert "tick_start" in kinds
    assert "tick_end" in kinds
    assert kinds[-1] == "end"


def test_slow_tick_warning_emitted(caplog):
    """A tree that sleeps in on_start triggers a slow tick warning."""
    import time

    class _SlowLeaf(TreeNode):
        def on_start(self):
            time.sleep(0.05)
            return Status.SUCCESS

        def on_running(self):
            return Status.SUCCESS

    tracer = _RecordingTracer()
    # Tight budget: anything over 10 ms is "slow".
    action = Action(
        tree=_SlowLeaf(), tracer=tracer, slow_tick_budget_s=0.01,
    )
    with caplog.at_level(logging.WARNING):
        action.tick(_empty_snapshot())
    slow_events = [e for e in tracer.events if e[0] == "slow_tick"]
    assert len(slow_events) == 1
    assert any("slow tick" in r.message for r in caplog.records)


def test_node_exception_event_fires_on_tracer():
    tracer = _RecordingTracer()
    action = Action(tree=_BoomLeaf(), tracer=tracer)
    action.tick(_empty_snapshot())
    exc_events = [e for e in tracer.events if e[0] == "node_exception"]
    assert len(exc_events) == 1
    assert exc_events[0][2] == "ValueError"


def test_log_tracer_does_not_blow_up():
    action = Action(tree=_ImmediateSuccess(), tracer=LogTracer())
    status = action.tick(_empty_snapshot())
    assert status == Status.SUCCESS
