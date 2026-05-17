from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np

from autoweaver.device.arm.base import ArmBase4
from autoweaver.device.arm.epson_ls6 import EpsonLS6
from autoweaver.device.arm.epson_ls6_worker import EpsonLS6Worker
from autoweaver.motion_policy.mock_runtime_client import MockRuntimeClient
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.async_pool import AsyncPool
from autoweaver.worker.base import TickContext


def _wire(worker: EpsonLS6Worker, board: WorldBoard) -> AsyncPool:
    """Inject board + minimal async pool the way BTClock would."""
    pool = AsyncPool(
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="t"),
        owns_executor=True,
    )
    worker._set_board(board)
    worker._set_async_pool(pool)
    return pool


def _ctx(tick_id: int = 0) -> TickContext:
    return TickContext(tick_id=tick_id, timestamp=0.0, dt=0.02)


# ─── Construction ──────────────────────────────────────────────────────────


def test_worker_owns_an_epson_ls6_driver():
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    assert isinstance(worker.driver, EpsonLS6)
    assert isinstance(worker.driver, ArmBase4)


def test_worker_driver_targets_correct_device():
    """Driver and worker must talk about the same device on the runtime."""
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="arm_x")
    worker.driver.move_l((1.0, 2.0, 3.0, 0.0))
    # Goal recorded on the right device, not the worker's display name.
    assert client.goals[0][1] == "ls6_1"


def test_worker_passes_speed_accel_to_driver():
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(
        client, device_name="ls6_1", name="ls6_1", speed=80, accel=150,
    )
    worker.driver.move_l((1.0, 0.0, 0.0, 0.0))
    fields = client.goals[0][3]
    assert fields["speed"] == 80
    assert fields["accel"] == 150


# ─── on_attach declares the 5 business-level state fields ──────────────────


def test_on_attach_declares_five_state_fields():
    board = WorldBoard()
    worker = EpsonLS6Worker(MockRuntimeClient(), device_name="ls6_1", name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_attach()
        declared = set(board.declared_states())
        assert "ls6_1.done" in declared
        assert "ls6_1.busy" in declared
        assert "ls6_1.error_code" in declared
        assert "ls6_1.pose" in declared
        assert "ls6_1.joints" in declared
    finally:
        pool.close()


def test_namespace_matches_worker_name():
    """Namespace is the Worker name (== device alias), not the runtime device id."""
    board = WorldBoard()
    worker = EpsonLS6Worker(MockRuntimeClient(), device_name="ls6_1", name="arm_left")
    pool = _wire(worker, board)
    try:
        worker.on_attach()
        declared = set(board.declared_states())
        assert "arm_left.done" in declared
        assert "ls6_1.done" not in declared
    finally:
        pool.close()


# ─── on_tick publishes status to the WorldBoard ────────────────────────────


def test_on_tick_publishes_done_busy_error_code():
    board = WorldBoard()
    client = MockRuntimeClient()
    client.preload_scara_status("ls6_1", done=True, busy=False, error_code=0)
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_attach()
        worker.on_tick(_ctx())
        assert board.read_state("ls6_1.done") is True
        assert board.read_state("ls6_1.busy") is False
        assert board.read_state("ls6_1.error_code") == 0
    finally:
        pool.close()


def test_on_tick_publishes_error_code_when_motion_failed():
    board = WorldBoard()
    client = MockRuntimeClient()
    client.preload_scara_status("ls6_1", error_code=1002)  # ERR_MOTION_FAILED
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_attach()
        worker.on_tick(_ctx())
        assert board.read_state("ls6_1.error_code") == 1002
    finally:
        pool.close()


def test_on_tick_publishes_pose_as_4x4_matrix():
    board = WorldBoard()
    client = MockRuntimeClient()
    client.preload_scara_status(
        "ls6_1", current_x=100.0, current_y=200.0, current_z=50.0, current_u=0.0,
    )
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_attach()
        worker.on_tick(_ctx())
        pose = board.read_state("ls6_1.pose")
        assert isinstance(pose, np.ndarray)
        assert pose.shape == (4, 4)
        assert np.allclose(pose[:3, 3], [100.0, 200.0, 50.0])
    finally:
        pool.close()


def test_on_tick_publishes_joints_as_tuple():
    board = WorldBoard()
    client = MockRuntimeClient()
    client.preload_scara_status(
        "ls6_1", joint_1=10.0, joint_2=20.0, joint_3=30.0, joint_4=40.0,
    )
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_attach()
        worker.on_tick(_ctx())
        joints = board.read_state("ls6_1.joints")
        assert joints == (10.0, 20.0, 30.0, 40.0)
    finally:
        pool.close()


# ─── Submit → tick → state reflects busy/done flip ─────────────────────────


def test_submit_then_tick_publishes_busy_true():
    """Driver submit flips mock status to busy=True; the next tick publishes that."""
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_attach()
        worker.driver.move_l((1.0, 2.0, 3.0, 0.0))
        worker.on_tick(_ctx())
        assert board.read_state("ls6_1.busy") is True
        assert board.read_state("ls6_1.done") is False
    finally:
        pool.close()


def test_completion_after_tick_publishes_done_true():
    """After complete_last_goal flips mock, the tick publishes done=True."""
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_attach()
        worker.driver.move_l((1.0, 2.0, 3.0, 0.0))
        client.complete_last_goal("ls6_1")
        worker.on_tick(_ctx())
        assert board.read_state("ls6_1.done") is True
        assert board.read_state("ls6_1.busy") is False
    finally:
        pool.close()


# ─── Two arms on one runtime, independent state ────────────────────────────


def test_two_workers_publish_under_separate_namespaces():
    board = WorldBoard()
    client = MockRuntimeClient()
    client.preload_scara_status("ls6_1", current_x=11.0)
    client.preload_scara_status("ls6_2", current_x=22.0)
    w1 = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    w2 = EpsonLS6Worker(client, device_name="ls6_2", name="ls6_2")
    pool1 = _wire(w1, board)
    pool2 = _wire(w2, board)
    try:
        w1.on_attach()
        w2.on_attach()
        w1.on_tick(_ctx())
        w2.on_tick(_ctx())
        p1 = board.read_state("ls6_1.pose")
        p2 = board.read_state("ls6_2.pose")
        assert p1[0, 3] == 11.0
        assert p2[0, 3] == 22.0
    finally:
        pool1.close()
        pool2.close()


# ─── note-based motion + request_id protocol ──────────────────────────────


def _wire_and_attach(worker: EpsonLS6Worker, board: WorldBoard):
    """Wire + run the lifecycle hooks BTClock would normally call."""
    pool = _wire(worker, board)
    worker._declare_framework_state()
    worker.on_attach()
    return pool


def test_move_l_note_dispatches_motion_to_runtime():
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire_and_attach(worker, board)
    try:
        board.pass_note(
            namespace="ls6_1",
            name="move_l",
            payload={"target": (100.0, 200.0, 50.0, 90.0), "__request_id__": 42},
            sender="test",
        )
        board.deliver_notes()
        # Driver was called; mock recorded a LINEAR goal.
        assert client.goals[0][2] == "LINEAR"
        assert client.goals[0][3]["x"] == 100.0
    finally:
        pool.close()


def test_move_l_note_tracks_pending_rid_and_completes_on_done_flip():
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire_and_attach(worker, board)
    try:
        # Dispatch the note: handler runs synchronously inside deliver_notes
        # and submits the motion (which flips mock status to busy=True).
        board.pass_note(
            namespace="ls6_1",
            name="move_l",
            payload={"target": (1.0, 2.0, 3.0, 0.0), "__request_id__": 7},
            sender="test",
        )
        board.deliver_notes()

        # last_request_id was recorded; last_completed_id is still 0
        # because the motion hasn't finished.
        assert board.read_state("ls6_1.last_request_id") == 7
        assert board.read_state("ls6_1.last_completed_id") == 0

        # Tick 1: worker sees busy=True → arms the completion detector.
        worker.on_tick(_ctx())
        assert board.read_state("ls6_1.busy") is True
        assert board.read_state("ls6_1.last_completed_id") == 0

        # Mock simulates the runtime finishing the motion.
        client.complete_last_goal("ls6_1")

        # Tick 2: worker sees busy=False + done=True → writes
        # last_completed_id with the pending rid.
        worker.on_tick(_ctx())
        assert board.read_state("ls6_1.last_completed_id") == 7
    finally:
        pool.close()


def test_consecutive_moves_dont_race_on_done_flag():
    """The exact bug the request_id protocol exists to prevent: a leaf
    dispatching move A then move B must wait for B's specific completion,
    not the stale done=True from A.
    """
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire_and_attach(worker, board)
    try:
        # Move 1: dispatch + complete.
        board.pass_note(
            namespace="ls6_1",
            name="move_l",
            payload={"target": (1.0, 0.0, 0.0, 0.0), "__request_id__": 10},
            sender="test",
        )
        board.deliver_notes()
        worker.on_tick(_ctx())  # observes busy=True
        client.complete_last_goal("ls6_1")
        worker.on_tick(_ctx())  # writes last_completed_id=10
        assert board.read_state("ls6_1.last_completed_id") == 10

        # Move 2: dispatch — done=True is still set from move 1.
        # The Worker must NOT immediately complete rid=11 based on that
        # stale done; it should wait for busy=True → busy=False of move 2.
        board.pass_note(
            namespace="ls6_1",
            name="move_l",
            payload={"target": (2.0, 0.0, 0.0, 0.0), "__request_id__": 11},
            sender="test",
        )
        board.deliver_notes()
        # Submit flipped mock status to busy=True, done=False.
        worker.on_tick(_ctx())
        # Tick observed busy=True; last_completed_id still 10.
        assert board.read_state("ls6_1.last_completed_id") == 10

        client.complete_last_goal("ls6_1")
        worker.on_tick(_ctx())
        # Now rid 11 is complete.
        assert board.read_state("ls6_1.last_completed_id") == 11
    finally:
        pool.close()


def test_error_code_completes_pending_request_and_writes_last_error():
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire_and_attach(worker, board)
    try:
        board.pass_note(
            namespace="ls6_1",
            name="move_l",
            payload={"target": (1.0, 0.0, 0.0, 0.0), "__request_id__": 99},
            sender="test",
        )
        board.deliver_notes()

        # Inject a controller-side error.
        client.preload_scara_status("ls6_1", error_code=1002, done=False, busy=True)

        worker.on_tick(_ctx())
        # The Worker surfaces the error and completes the pending rid
        # so the BT's NotifyAndWait doesn't hang.
        assert board.read_state("ls6_1.error_code") == 1002
        assert board.read_state("ls6_1.last_completed_id") == 99
        assert "error_code=1002" in board.read_state("ls6_1.last_error")
    finally:
        pool.close()


def test_move_j_note_dispatches_via_go():
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire_and_attach(worker, board)
    try:
        board.pass_note(
            namespace="ls6_1",
            name="move_j",
            payload={"target": (1.0, 2.0, 3.0, 4.0), "__request_id__": 1},
            sender="test",
        )
        board.deliver_notes()
        assert client.goals[0][2] == "GO"
    finally:
        pool.close()


def test_jump_note_dispatches_jump():
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire_and_attach(worker, board)
    try:
        board.pass_note(
            namespace="ls6_1",
            name="jump",
            payload={"target": (1.0, 2.0, 3.0, 4.0), "__request_id__": 1},
            sender="test",
        )
        board.deliver_notes()
        assert client.goals[0][2] == "JUMP"
    finally:
        pool.close()


def test_speed_and_accel_in_payload_override_default():
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(
        client, device_name="ls6_1", name="ls6_1", speed=50, accel=200,
    )
    pool = _wire_and_attach(worker, board)
    try:
        board.pass_note(
            namespace="ls6_1",
            name="move_l",
            payload={
                "target": (1.0, 0.0, 0.0, 0.0),
                "speed": 80,
                "accel": 300,
                "__request_id__": 1,
            },
            sender="test",
        )
        board.deliver_notes()
        fields = client.goals[0][3]
        assert fields["speed"] == 80
        assert fields["accel"] == 300
    finally:
        pool.close()


def test_halt_note_completes_pending_request():
    """halt mid-motion must release any leaf waiting on the pending rid."""
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire_and_attach(worker, board)
    try:
        board.pass_note(
            namespace="ls6_1",
            name="move_l",
            payload={"target": (1.0, 0.0, 0.0, 0.0), "__request_id__": 5},
            sender="test",
        )
        board.deliver_notes()
        # Halt before the motion would naturally complete.
        board.pass_note(
            namespace="ls6_1", name="halt", payload={}, sender="test",
        )
        board.deliver_notes()
        assert board.read_state("ls6_1.last_completed_id") == 5
    finally:
        pool.close()


def test_payload_missing_target_writes_error_and_completes_rid():
    board = WorldBoard()
    client = MockRuntimeClient()
    worker = EpsonLS6Worker(client, device_name="ls6_1", name="ls6_1")
    pool = _wire_and_attach(worker, board)
    try:
        board.pass_note(
            namespace="ls6_1",
            name="move_l",
            payload={"__request_id__": 1},  # no target
            sender="test",
        )
        board.deliver_notes()
        # No goal was submitted to the runtime.
        assert client.goals == []
        # But the request was completed so the BT doesn't hang.
        assert board.read_state("ls6_1.last_completed_id") == 1
        assert "target" in board.read_state("ls6_1.last_error")
    finally:
        pool.close()
