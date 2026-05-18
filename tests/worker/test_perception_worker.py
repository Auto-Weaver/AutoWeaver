"""Tests for PerceptionWorker — synchronous-completion protocol."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.async_pool import AsyncPool
from autoweaver.worker.base import TickContext, WorkerState, next_request_id
from autoweaver.worker.perception import PerceptionWorker


class _MinimalPerception(PerceptionWorker):
    def __init__(self, name: str = "demo"):
        super().__init__()
        self._name = name

    @property
    def name(self) -> str:
        return self._name


def _wire(worker: PerceptionWorker, board: WorldBoard) -> AsyncPool:
    pool = AsyncPool(
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="t"),
        owns_executor=True,
    )
    worker._set_board(board)
    worker._set_async_pool(pool)
    worker._declare_framework_state()
    return pool


# ---- accept_notes basics ------------------------------------------------

def test_accept_notes_registers_under_own_namespace():
    board = WorldBoard()
    worker = _MinimalPerception("demo")
    pool = _wire(worker, board)
    try:
        received = []
        worker.accept_notes("hello", dict, received.append)

        board.pass_note("demo", "hello", {"x": 1}, sender="bt")
        board.deliver_notes()
        assert received == [{"x": 1}]
    finally:
        pool.close()


# ---- request_id protocol ------------------------------------------------

def test_handler_return_writes_last_completed_id():
    board = WorldBoard()
    worker = _MinimalPerception("demo")
    pool = _wire(worker, board)
    try:
        worker.accept_notes("hello", dict, lambda p: None)

        rid = next_request_id()
        board.pass_note("demo", "hello", {"__request_id__": rid}, sender="bt")
        board.deliver_notes()

        assert board.read_state("demo.last_request_id") == rid
        assert board.read_state("demo.last_completed_id") == rid
    finally:
        pool.close()


def test_handler_exception_faults_worker_and_records_error():
    board = WorldBoard()
    worker = _MinimalPerception("demo")
    pool = _wire(worker, board)
    try:
        def bad(payload):
            raise RuntimeError("boom")

        worker.accept_notes("hello", dict, bad)

        rid = next_request_id()
        board.pass_note("demo", "hello", {"__request_id__": rid}, sender="bt")
        board.deliver_notes()

        assert worker.lifecycle_state is WorkerState.FAULTED
        assert "boom" in board.read_state("demo.last_error")
        # Handler raised → last_completed_id NOT advanced.
        assert board.read_state("demo.last_completed_id") == 0
    finally:
        pool.close()
