from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np

from autoweaver.device.arm.dobot.driver import Dobot
from autoweaver.device.arm.dobot.states import (
    ROBOT_MODE_ENABLE,
    ROBOT_MODE_ERROR,
    ROBOT_MODE_RUNNING,
)
from autoweaver.device.arm.dobot.worker import DobotWorker
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.async_pool import AsyncPool
from autoweaver.worker.base import TickContext


# ─── Fake Dobot SDK pieces ─────────────────────────────────────────────────


class _FakeDashboard:
    def __init__(self):
        self.calls: list[tuple] = []

    def MovJ(self, *args, **kwargs):
        self.calls.append(("MovJ", args, kwargs))

    def MovL(self, *args, **kwargs):
        self.calls.append(("MovL", args, kwargs))

    def Stop(self):
        self.calls.append(("Stop",))


class _FakeFeedback:
    """Returns whatever frame _current points at."""

    def __init__(self):
        self.frame: dict | None = _frame(ROBOT_MODE_ENABLE)

    def feedBackData(self):
        if self.frame is None:
            return None
        return [self.frame]


def _frame(robot_mode: int, *, pose=(0, 0, 0, 0, 0, 0), q=(0, 0, 0, 0, 0, 0)) -> dict:
    return {
        "RobotMode": robot_mode,
        "ToolVectorActual": pose,
        "QActual": q,
    }


def _wire(worker: DobotWorker, board: WorldBoard) -> AsyncPool:
    pool = AsyncPool(
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="t"),
        owns_executor=True,
    )
    worker._set_board(board)
    worker._set_async_pool(pool)
    return pool


def _attached(worker: DobotWorker, board: WorldBoard) -> AsyncPool:
    """Wire + framework + on_attach. Skip on_start (no real sockets)."""
    pool = _wire(worker, board)
    worker._declare_framework_state()
    worker.on_attach()
    return pool


def _make_worker(robot_mode: int = ROBOT_MODE_ENABLE) -> tuple[DobotWorker, _FakeDashboard, _FakeFeedback]:
    """Build a DobotWorker with fake SDK pieces installed."""
    worker = DobotWorker(ip="127.0.0.1", name="nova5")
    dashboard = _FakeDashboard()
    feedback = _FakeFeedback()
    feedback.frame = _frame(robot_mode)
    worker.driver._dashboard = dashboard  # type: ignore[assignment]
    worker.driver._feedback = feedback    # type: ignore[assignment]
    return worker, dashboard, feedback


def _ctx() -> TickContext:
    return TickContext(tick_id=0, timestamp=0.0, dt=0.02)


# ─── Lifecycle / construction ──────────────────────────────────────────────


def test_worker_owns_a_dobot_driver():
    worker = DobotWorker(ip="127.0.0.1", name="nova5")
    assert isinstance(worker.driver, Dobot)


def test_worker_name_matches_construction():
    worker = DobotWorker(ip="127.0.0.1", name="nova_left")
    assert worker.name == "nova_left"


# ─── on_attach declares 5 business-level state fields ──────────────────────


def test_on_attach_declares_state_fields():
    worker, _, _ = _make_worker()
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        declared = set(board.declared_states())
        assert "nova5.done" in declared
        assert "nova5.busy" in declared
        assert "nova5.error_code" in declared
        assert "nova5.pose" in declared
        assert "nova5.joints" in declared
    finally:
        pool.close()


# ─── on_tick publishes feedback frame contents ─────────────────────────────


def test_on_tick_publishes_pose_as_4x4_matrix():
    worker, _, feedback = _make_worker()
    feedback.frame = _frame(ROBOT_MODE_ENABLE, pose=(100.0, 200.0, 50.0, 0.0, 0.0, 0.0))
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        worker.on_tick(_ctx())
        pose = board.read_state("nova5.pose")
        assert isinstance(pose, np.ndarray)
        assert pose.shape == (4, 4)
        assert np.allclose(pose[:3, 3], [100.0, 200.0, 50.0])
    finally:
        pool.close()


def test_on_tick_publishes_joints_tuple():
    worker, _, feedback = _make_worker()
    feedback.frame = _frame(ROBOT_MODE_ENABLE, q=(10, 20, 30, 40, 50, 60))
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        worker.on_tick(_ctx())
        joints = board.read_state("nova5.joints")
        assert joints == (10.0, 20.0, 30.0, 40.0, 50.0, 60.0)
    finally:
        pool.close()


def test_robot_mode_running_publishes_busy_true():
    worker, _, feedback = _make_worker()
    feedback.frame = _frame(ROBOT_MODE_RUNNING)
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        worker.on_tick(_ctx())
        assert board.read_state("nova5.busy") is True
        assert board.read_state("nova5.done") is False
    finally:
        pool.close()


def test_robot_mode_enable_publishes_done_true():
    worker, _, feedback = _make_worker()
    feedback.frame = _frame(ROBOT_MODE_ENABLE)
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        worker.on_tick(_ctx())
        assert board.read_state("nova5.done") is True
        assert board.read_state("nova5.busy") is False
    finally:
        pool.close()


def test_robot_mode_error_publishes_error_code_nonzero():
    worker, _, feedback = _make_worker()
    feedback.frame = _frame(ROBOT_MODE_ERROR)
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        worker.on_tick(_ctx())
        assert board.read_state("nova5.error_code") == ROBOT_MODE_ERROR
        assert board.read_state("nova5.done") is False
    finally:
        pool.close()


def test_no_feedback_frame_is_silently_skipped():
    """If the SDK feedback stream hasn't delivered a frame yet, on_tick
    must not raise — just skip and try next tick."""
    worker, _, feedback = _make_worker()
    feedback.frame = None
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        worker.on_tick(_ctx())  # must not raise
    finally:
        pool.close()


# ─── Note-based motion + completion ────────────────────────────────────────


def test_move_l_note_dispatches_to_driver():
    worker, dashboard, _ = _make_worker()
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        board.pass_note(
            namespace="nova5",
            name="move_l",
            payload={
                "target": (10, 20, 30, 0, 0, 0),
                "__request_id__": 1,
            },
            sender="test",
        )
        board.deliver_notes()
        # MovL was called.
        assert dashboard.calls[0][0] == "MovL"
    finally:
        pool.close()


def test_busy_true_to_false_transition_writes_last_completed_id():
    worker, _, feedback = _make_worker()
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        board.pass_note(
            namespace="nova5",
            name="move_l",
            payload={"target": (1, 0, 0, 0, 0, 0), "__request_id__": 42},
            sender="test",
        )
        board.deliver_notes()

        # Tick 1: controller is RUNNING → busy=True observed.
        feedback.frame = _frame(ROBOT_MODE_RUNNING)
        worker.on_tick(_ctx())
        assert board.read_state("nova5.last_completed_id") == 0

        # Tick 2: still RUNNING — not yet complete.
        worker.on_tick(_ctx())
        assert board.read_state("nova5.last_completed_id") == 0

        # Tick 3: motion finished, controller back to ENABLE.
        feedback.frame = _frame(ROBOT_MODE_ENABLE)
        worker.on_tick(_ctx())
        assert board.read_state("nova5.last_completed_id") == 42
    finally:
        pool.close()


def test_consecutive_moves_dont_race_on_done():
    """Two move_l notes in a row: rid2 must not see rid1's completion
    as its own — the no-op fallback would NOT trigger (we control
    feedback frames explicitly here)."""
    worker, _, feedback = _make_worker()
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        # Move 1
        board.pass_note(
            namespace="nova5", name="move_l",
            payload={"target": (1, 0, 0, 0, 0, 0), "__request_id__": 10},
            sender="test",
        )
        board.deliver_notes()
        feedback.frame = _frame(ROBOT_MODE_RUNNING)
        worker.on_tick(_ctx())
        feedback.frame = _frame(ROBOT_MODE_ENABLE)
        worker.on_tick(_ctx())
        assert board.read_state("nova5.last_completed_id") == 10

        # Move 2 — controller starts in ENABLE (done from previous move),
        # then transitions to RUNNING. Worker must not complete rid 11
        # before observing busy=True for rid 11 specifically.
        board.pass_note(
            namespace="nova5", name="move_l",
            payload={"target": (2, 0, 0, 0, 0, 0), "__request_id__": 11},
            sender="test",
        )
        board.deliver_notes()
        # Feedback is still ENABLE (done). Worker counts this as no-op
        # tick; pending rid stays 11.
        worker.on_tick(_ctx())
        assert board.read_state("nova5.last_completed_id") == 10

        feedback.frame = _frame(ROBOT_MODE_RUNNING)
        worker.on_tick(_ctx())
        feedback.frame = _frame(ROBOT_MODE_ENABLE)
        worker.on_tick(_ctx())
        assert board.read_state("nova5.last_completed_id") == 11
    finally:
        pool.close()


def test_robot_mode_error_during_motion_completes_pending_rid():
    worker, _, feedback = _make_worker()
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        board.pass_note(
            namespace="nova5", name="move_l",
            payload={"target": (1, 0, 0, 0, 0, 0), "__request_id__": 99},
            sender="test",
        )
        board.deliver_notes()
        feedback.frame = _frame(ROBOT_MODE_RUNNING)
        worker.on_tick(_ctx())

        # Controller trips into ERROR (workspace limit, joint limit, etc.)
        feedback.frame = _frame(ROBOT_MODE_ERROR)
        worker.on_tick(_ctx())

        assert board.read_state("nova5.last_completed_id") == 99
        assert "alarm" in board.read_state("nova5.last_error")
    finally:
        pool.close()


def test_no_op_completion_after_threshold_ticks():
    """If busy never goes True (target == current pose), the worker
    treats the move as already complete after the grace period."""
    worker, _, feedback = _make_worker()
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        board.pass_note(
            namespace="nova5", name="move_l",
            payload={"target": (0, 0, 0, 0, 0, 0), "__request_id__": 7},
            sender="test",
        )
        board.deliver_notes()
        # Feedback stays ENABLE forever — controller skipped the motion.
        feedback.frame = _frame(ROBOT_MODE_ENABLE)
        # Tick many times; on_tick should eventually complete the request.
        for _ in range(40):
            worker.on_tick(_ctx())
        assert board.read_state("nova5.last_completed_id") == 7
    finally:
        pool.close()


def test_halt_note_completes_pending_request():
    worker, _, feedback = _make_worker()
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        board.pass_note(
            namespace="nova5", name="move_l",
            payload={"target": (1, 0, 0, 0, 0, 0), "__request_id__": 5},
            sender="test",
        )
        board.deliver_notes()
        feedback.frame = _frame(ROBOT_MODE_RUNNING)
        worker.on_tick(_ctx())

        # Halt before natural completion.
        board.pass_note(
            namespace="nova5", name="halt", payload={}, sender="test",
        )
        board.deliver_notes()
        assert board.read_state("nova5.last_completed_id") == 5
    finally:
        pool.close()


def test_move_j_joints_note_dispatches_with_joint_mode():
    worker, dashboard, _ = _make_worker()
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        board.pass_note(
            namespace="nova5", name="move_j_joints",
            payload={"target": (10, 20, 30, 40, 50, 60), "__request_id__": 1},
            sender="test",
        )
        board.deliver_notes()
        # MovJ with COORD_JOINT (1) at the end.
        name, args, _kwargs = dashboard.calls[0]
        assert name == "MovJ"
        assert args[-1] == 1  # COORD_JOINT
    finally:
        pool.close()


def test_payload_missing_target_writes_error_and_completes_rid():
    worker, dashboard, _ = _make_worker()
    board = WorldBoard()
    pool = _attached(worker, board)
    try:
        board.pass_note(
            namespace="nova5", name="move_l",
            payload={"__request_id__": 1},
            sender="test",
        )
        board.deliver_notes()
        # No move was sent.
        assert dashboard.calls == []
        assert board.read_state("nova5.last_completed_id") == 1
        assert "target" in board.read_state("nova5.last_error")
    finally:
        pool.close()
