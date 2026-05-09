"""Tests for BTClock — tick orchestration, lifecycle, isolation."""

from __future__ import annotations

import threading
import time

import pytest

from autoweaver.motion_policy.action import Action
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.subsystem.async_pool import AsyncPoolRegistry
from autoweaver.subsystem.base import Subsystem, SubsystemState, TickContext
from autoweaver.subsystem.clock import BTClock


# ---- Fixtures -----------------------------------------------------------

class _RecordingSubsystem(Subsystem):
    def __init__(self, name: str = "rec"):
        super().__init__()
        self._n = name
        self.attached = False
        self.started = False
        self.stopped = False
        self.detached = False
        self.tick_ids: list[int] = []
        self.fail_on_tick: int | None = None
        self.fail_on_start: bool = False

    @property
    def name(self) -> str:
        return self._n

    def on_attach(self) -> None:
        self.attached = True

    def on_start(self) -> None:
        self.started = True
        if self.fail_on_start:
            raise RuntimeError("start failed")

    def on_tick(self, ctx: TickContext) -> None:
        if self.fail_on_tick is not None and ctx.tick_id == self.fail_on_tick:
            raise RuntimeError(f"tick {ctx.tick_id} failed")
        self.tick_ids.append(ctx.tick_id)

    def on_stop(self) -> None:
        self.stopped = True

    def on_detach(self) -> None:
        self.detached = True


class _ImmediateSuccess(TreeNode):
    def on_start(self) -> Status:
        return Status.SUCCESS

    def on_running(self) -> Status:
        return Status.SUCCESS


class _CountingTree(TreeNode):
    def __init__(self):
        super().__init__()
        self.tick_count = 0

    def on_start(self) -> Status:
        self.tick_count += 1
        return Status.RUNNING

    def on_running(self) -> Status:
        self.tick_count += 1
        return Status.RUNNING


# ---- Subsystem lifecycle -----------------------------------------------

def test_attach_calls_on_attach_then_on_start():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    sub = _RecordingSubsystem()
    try:
        clock.attach_subsystem(sub)
        assert sub.attached is True
        assert sub.started is True
        assert sub.lifecycle_state is SubsystemState.RUNNING
    finally:
        clock.shutdown()


def test_detach_calls_on_stop_then_on_detach():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    sub = _RecordingSubsystem()
    try:
        clock.attach_subsystem(sub)
        clock.detach_subsystem(sub)
        assert sub.stopped is True
        assert sub.detached is True
        assert sub.lifecycle_state is SubsystemState.UNATTACHED
    finally:
        clock.shutdown()


def test_attach_failure_marks_faulted_and_calls_on_stop():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    sub = _RecordingSubsystem()
    sub.fail_on_start = True
    try:
        with pytest.raises(RuntimeError, match="start failed"):
            clock.attach_subsystem(sub)
        # Even after failure, on_stop must have run for cleanup.
        assert sub.stopped is True
        assert sub.lifecycle_state is SubsystemState.FAULTED
    finally:
        clock.shutdown()


def test_double_attach_raises():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    sub = _RecordingSubsystem()
    try:
        clock.attach_subsystem(sub)
        with pytest.raises(RuntimeError, match="UNATTACHED"):
            clock.attach_subsystem(sub)
    finally:
        clock.shutdown()


# ---- tick_once: subsystem broadcast ------------------------------------

def test_tick_once_broadcasts_to_running_subsystems():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    sub = _RecordingSubsystem()
    try:
        clock.attach_subsystem(sub)
        clock.tick_once()
        clock.tick_once()
        clock.tick_once()
        assert sub.tick_ids == [0, 1, 2]
    finally:
        clock.shutdown()


def test_tick_id_monotonic_across_ticks():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    try:
        ctx_a = clock.tick_once()
        ctx_b = clock.tick_once()
        assert ctx_a.tick_id == 0
        assert ctx_b.tick_id == 1
        assert ctx_b.timestamp >= ctx_a.timestamp
    finally:
        clock.shutdown()


def test_subsystem_exception_marks_faulted_and_isolates():
    """One subsystem raising during on_tick must not affect others."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    bad = _RecordingSubsystem(name="bad")
    bad.fail_on_tick = 0
    good = _RecordingSubsystem(name="good")
    try:
        clock.attach_subsystem(bad)
        clock.attach_subsystem(good)
        clock.tick_once()
        assert bad.lifecycle_state is SubsystemState.FAULTED
        assert good.tick_ids == [0]

        # Subsequent ticks: bad is no longer in RUNNING, skipped.
        clock.tick_once()
        assert bad.tick_ids == []  # never recorded
        assert good.tick_ids == [0, 1]
    finally:
        clock.shutdown()


def test_paused_subsystem_does_not_receive_tick():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    sub = _RecordingSubsystem()
    try:
        clock.attach_subsystem(sub)
        clock.tick_once()
        clock.pause_subsystem(sub)
        clock.tick_once()
        clock.tick_once()
        assert sub.lifecycle_state is SubsystemState.PAUSED
        assert sub.tick_ids == [0]  # only the pre-pause tick

        clock.resume_subsystem(sub)
        clock.tick_once()
        assert sub.tick_ids == [0, 3]
    finally:
        clock.shutdown()


# ---- tick_once: tree dispatch ------------------------------------------

def test_attached_tree_receives_ticks():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    tree = _CountingTree()
    action = Action(tree=tree)
    try:
        clock.attach_tree(action, name="t1")
        clock.tick_once()
        clock.tick_once()
        assert tree.tick_count == 2
    finally:
        clock.shutdown()


def test_tree_terminal_status_is_idempotent_under_clock():
    """Trees that finish stay finished — no double-execution by the clock."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    counter = {"n": 0}

    class _OnceSuccess(TreeNode):
        def on_start(self) -> Status:
            counter["n"] += 1
            return Status.SUCCESS

        def on_running(self) -> Status:
            counter["n"] += 1
            return Status.SUCCESS

    action = Action(tree=_OnceSuccess())
    try:
        clock.attach_tree(action)
        for _ in range(5):
            clock.tick_once()
        assert counter["n"] == 1
    finally:
        clock.shutdown()


def test_detach_tree_halts_and_skips_in_subsequent_ticks():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    tree = _CountingTree()
    action = Action(tree=tree)
    try:
        handle = clock.attach_tree(action)
        clock.tick_once()
        assert tree.tick_count == 1

        clock.detach_tree(handle)
        clock.tick_once()
        clock.tick_once()
        # Detached tree no longer gets ticked.
        assert tree.tick_count == 1
    finally:
        clock.shutdown()


# ---- tick_once: ordering -----------------------------------------------

def test_tick_order_drains_then_delivers_then_trees_then_subsystems():
    """The four phases must run in order:
        1. drain on_done queue
        2. deliver_notes (pending notes from previous tick)
        3. tick all trees
        4. broadcast tick to subsystems"""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    events: list[str] = []

    class _OrderingTree(TreeNode):
        def on_start(self) -> Status:
            events.append("tree_tick")
            return Status.RUNNING

        def on_running(self) -> Status:
            events.append("tree_tick")
            return Status.RUNNING

    class _OrderingSub(Subsystem):
        @property
        def name(self) -> str:
            return "order"

        def on_attach(self) -> None:
            self.accept_notes(
                "ping", dict, lambda p: events.append("note_received"),
            )

        def on_tick(self, ctx: TickContext) -> None:
            events.append("sub_tick")

    sub = _OrderingSub()
    action = Action(tree=_OrderingTree())
    try:
        clock.attach_tree(action)
        clock.attach_subsystem(sub)

        # Pass a note before the first tick — it must arrive before the
        # tree tick and the sub tick.
        board.pass_note("order", "ping", {}, sender="test")
        clock.tick_once()

        assert events == ["note_received", "tree_tick", "sub_tick"]
    finally:
        clock.shutdown()


def test_pass_note_during_tree_tick_delivers_on_next_tick():
    """A note passed during a tree's tick is held until the *next* tick's
    deliver phase. This is the half-tick delay called out in EVO-006."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    received: list[int] = []

    class _Sender(TreeNode):
        def __init__(self):
            super().__init__()
            self.sent = 0

        def on_start(self) -> Status:
            self.sent += 1
            board.pass_note("rcv", "ping", {"i": self.sent}, sender="tree")
            return Status.RUNNING

        def on_running(self) -> Status:
            self.sent += 1
            board.pass_note("rcv", "ping", {"i": self.sent}, sender="tree")
            return Status.RUNNING

    class _Receiver(Subsystem):
        @property
        def name(self) -> str:
            return "rcv"

        def on_attach(self) -> None:
            self.accept_notes("ping", dict, received.append)

        def on_tick(self, ctx: TickContext) -> None:
            pass

    action = Action(tree=_Sender())
    sub = _Receiver()
    try:
        clock.attach_subsystem(sub)
        clock.attach_tree(action)
        # Tick 1: tree sends note #1; received is empty (no prior tick to deliver).
        clock.tick_once()
        assert received == []
        # Tick 2: deliver_notes sends #1; tree sends #2.
        clock.tick_once()
        assert received == [{"i": 1}]
        # Tick 3: deliver #2.
        clock.tick_once()
        assert received == [{"i": 1}, {"i": 2}]
    finally:
        clock.shutdown()


# ---- run_async integration ---------------------------------------------

def test_run_async_callback_fires_on_subsequent_tick():
    """on_done queued during work runs on the *next* tick's drain phase."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    results: list[int] = []

    class _Worker(Subsystem):
        def __init__(self):
            super().__init__()
            self.kicked = False

        @property
        def name(self) -> str:
            return "worker"

        def on_tick(self, ctx: TickContext) -> None:
            if not self.kicked:
                self.kicked = True
                self.run_async(lambda: 7, on_done=results.append)

    sub = _Worker()
    try:
        clock.attach_subsystem(sub)

        # Tick 1 kicks off work; worker runs on a thread.
        clock.tick_once()
        # Wait for worker to complete and queue on_done.
        time.sleep(0.05)
        # Tick 2 should see results delivered via drain (phase 1).
        clock.tick_once()
        assert results == [7]
    finally:
        clock.shutdown()


# ---- run() / stop() ----------------------------------------------------

def test_run_blocks_until_stop():
    board = WorldBoard()
    clock = BTClock(world_board=board, hz=200)
    sub = _RecordingSubsystem()
    try:
        clock.attach_subsystem(sub)

        thread = threading.Thread(target=clock.run, daemon=True)
        thread.start()
        time.sleep(0.05)
        clock.stop()
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        # At ~200 Hz over 50ms we expect a handful of ticks.
        assert len(sub.tick_ids) >= 2
    finally:
        clock.shutdown()


# ---- Introspection -----------------------------------------------------

def test_attached_subsystems_lists_only_attached():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    a = _RecordingSubsystem(name="a")
    b = _RecordingSubsystem(name="b")
    try:
        clock.attach_subsystem(a)
        clock.attach_subsystem(b)
        assert sorted(clock.attached_subsystems()) == ["a", "b"]
        clock.detach_subsystem(a)
        assert clock.attached_subsystems() == ["b"]
    finally:
        clock.shutdown()


def test_attached_trees_lists_attached_only():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    a = Action(tree=_ImmediateSuccess(), name="ok")
    try:
        clock.attach_tree(a)
        assert clock.attached_trees() == ["ok"]
    finally:
        clock.shutdown()
