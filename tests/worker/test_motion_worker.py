"""Tests for MotionWorker — tick-async completion protocol."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.async_pool import AsyncPool
from autoweaver.worker.base import TickContext, WorkerState, next_request_id
from autoweaver.worker.motion import MotionWorker


class _MinimalMotion(MotionWorker):
    """Test MotionWorker that records dispatch calls."""

    def __init__(self, name: str = "arm"):
        super().__init__()
        self._name = name
        self.dispatched: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    def dispatch_ok(self, payload: dict) -> None:
        self.dispatched.append(payload)

    def dispatch_raises(self, payload: dict) -> None:
        raise RuntimeError("boom")


def _wire(worker: MotionWorker, board: WorldBoard) -> AsyncPool:
    pool = AsyncPool(
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="t"),
        owns_executor=True,
    )
    worker._set_board(board)
    worker._set_async_pool(pool)
    worker._declare_framework_state()
    return pool


# ---- dispatch + completion edge ----------------------------------------

def test_full_motion_lifecycle_one_request():
    """dispatch → busy=True → busy=False → last_completed_id."""
    board = WorldBoard()
    worker = _MinimalMotion("arm")
    pool = _wire(worker, board)
    try:
        worker.accept_async_notes("move_l", dict, worker.dispatch_ok)

        rid = next_request_id()
        board.pass_note(
            "arm", "move_l",
            {"__request_id__": rid, "target": (1, 2, 3, 0)},
            sender="bt",
        )
        board.deliver_notes()

        # dispatch saw the payload without __request_id__
        assert worker.dispatched == [{"target": (1, 2, 3, 0)}]
        assert board.read_state("arm.last_request_id") == rid
        assert board.read_state("arm.last_completed_id") == 0   # not done yet
        assert worker._pending_request_id == rid

        # Tick 1: hardware reports busy=True
        worker.note_busy_started()
        assert worker._move_started

        # Tick 2: hardware reports busy=False, done=True
        worker.note_completion()
        assert board.read_state("arm.last_completed_id") == rid
        assert worker._pending_request_id is None
    finally:
        pool.close()


def test_completion_before_busy_is_ignored():
    """Stale done from before dispatch must not falsely complete."""
    board = WorldBoard()
    worker = _MinimalMotion("arm")
    pool = _wire(worker, board)
    try:
        worker.accept_async_notes("move_l", dict, worker.dispatch_ok)

        rid = next_request_id()
        board.pass_note(
            "arm", "move_l",
            {"__request_id__": rid, "target": (0, 0, 0, 0)},
            sender="bt",
        )
        board.deliver_notes()

        # Stale done — _move_started is still False
        worker.note_completion()
        assert board.read_state("arm.last_completed_id") == 0  # not advanced
        assert worker._pending_request_id == rid
    finally:
        pool.close()


def test_error_completes_request_without_faulting():
    board = WorldBoard()
    worker = _MinimalMotion("arm")
    pool = _wire(worker, board)
    try:
        worker.accept_async_notes("move_l", dict, worker.dispatch_ok)

        rid = next_request_id()
        board.pass_note(
            "arm", "move_l", {"__request_id__": rid, "target": (1,)},
            sender="bt",
        )
        board.deliver_notes()
        worker.note_busy_started()

        worker.note_error("workspace limit")

        assert worker.lifecycle_state is not WorkerState.FAULTED
        assert "workspace limit" in board.read_state("arm.last_error")
        # BT must not hang: last_completed_id advanced.
        assert board.read_state("arm.last_completed_id") == rid
        assert worker._pending_request_id is None
    finally:
        pool.close()


# ---- overlap force-completes the old request ---------------------------

def test_overlap_force_completes_old_request():
    board = WorldBoard()
    worker = _MinimalMotion("arm")
    pool = _wire(worker, board)
    try:
        worker.accept_async_notes("move_l", dict, worker.dispatch_ok)

        rid1 = next_request_id()
        board.pass_note(
            "arm", "move_l", {"__request_id__": rid1, "target": (1,)},
            sender="bt",
        )
        board.deliver_notes()
        # No busy seen yet; new motion arrives anyway
        rid2 = next_request_id()
        board.pass_note(
            "arm", "move_l", {"__request_id__": rid2, "target": (2,)},
            sender="bt",
        )
        board.deliver_notes()

        # Old rid is force-completed; new one is pending
        assert board.read_state("arm.last_completed_id") == rid1
        assert worker._pending_request_id == rid2
    finally:
        pool.close()


# ---- dispatch exception → last_error + completion, NOT FAULTED ---------

def test_dispatch_exception_does_not_fault_worker():
    board = WorldBoard()
    worker = _MinimalMotion("arm")
    pool = _wire(worker, board)
    try:
        worker.accept_async_notes("move_l", dict, worker.dispatch_raises)

        rid = next_request_id()
        board.pass_note(
            "arm", "move_l", {"__request_id__": rid, "target": (1,)},
            sender="bt",
        )
        board.deliver_notes()

        assert worker.lifecycle_state is not WorkerState.FAULTED
        assert "boom" in board.read_state("arm.last_error")
        assert board.read_state("arm.last_completed_id") == rid
        assert worker._pending_request_id is None
    finally:
        pool.close()


# ---- no-op grace --------------------------------------------------------

def test_no_op_grace_auto_completes_after_threshold():
    """no_op_tick_threshold=3 → 3 idle ticks → auto completion."""
    board = WorldBoard()

    class FastNoOp(_MinimalMotion):
        no_op_tick_threshold = 3

    worker = FastNoOp("arm")
    pool = _wire(worker, board)
    try:
        worker.accept_async_notes("move_l", dict, worker.dispatch_ok)

        rid = next_request_id()
        board.pass_note(
            "arm", "move_l", {"__request_id__": rid, "target": (0,)},
            sender="bt",
        )
        board.deliver_notes()

        # 2 idle ticks: not yet
        worker.note_idle_tick()
        worker.note_idle_tick()
        assert board.read_state("arm.last_completed_id") == 0

        # 3rd idle tick triggers auto-complete
        worker.note_idle_tick()
        assert board.read_state("arm.last_completed_id") == rid
        assert worker._pending_request_id is None
    finally:
        pool.close()


def test_no_op_grace_disabled_by_default():
    """no_op_tick_threshold=0 → note_idle_tick is a no-op."""
    board = WorldBoard()
    worker = _MinimalMotion("arm")
    assert worker.no_op_tick_threshold == 0
    pool = _wire(worker, board)
    try:
        worker.accept_async_notes("move_l", dict, worker.dispatch_ok)

        rid = next_request_id()
        board.pass_note(
            "arm", "move_l", {"__request_id__": rid, "target": (0,)},
            sender="bt",
        )
        board.deliver_notes()

        for _ in range(100):
            worker.note_idle_tick()
        assert board.read_state("arm.last_completed_id") == 0
        assert worker._pending_request_id == rid
    finally:
        pool.close()


# ---- cancel_pending -----------------------------------------------------

def test_cancel_pending_completes_without_error():
    board = WorldBoard()
    worker = _MinimalMotion("arm")
    pool = _wire(worker, board)
    try:
        worker.accept_async_notes("move_l", dict, worker.dispatch_ok)

        rid = next_request_id()
        board.pass_note(
            "arm", "move_l", {"__request_id__": rid, "target": (1,)},
            sender="bt",
        )
        board.deliver_notes()
        worker.note_busy_started()

        worker.cancel_pending(reason="halt")

        assert board.read_state("arm.last_completed_id") == rid
        # cancel is not an error — last_error stays empty
        assert board.read_state("arm.last_error") == ""
        assert worker._pending_request_id is None
    finally:
        pool.close()
