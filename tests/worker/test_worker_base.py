"""Tests for Worker base class — convenience API and lifecycle helpers.

Note: full lifecycle integration with BTClock is tested in test_clock.py.
This module focuses on the subclass-facing API in isolation.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.async_pool import AsyncPool
from autoweaver.worker.base import (
    AsyncPoolConfig,
    TickContext,
    Worker,
    WorkerState,
)


# ---- Fixtures -----------------------------------------------------------

class _MinimalWorker(Worker):
    """Bare-bones concrete Worker for protocol tests."""

    def __init__(self, name: str = "demo"):
        super().__init__()
        self._name = name
        self.tick_count = 0

    @property
    def name(self) -> str:
        return self._name

    def on_tick(self, ctx: TickContext) -> None:
        self.tick_count += 1


def _wire_worker(worker: Worker, board: WorldBoard) -> AsyncPool:
    """Inject board + a minimal pool the way BTClock would."""
    pool = AsyncPool(
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="t"),
        owns_executor=True,
    )
    worker._set_board(board)
    worker._set_async_pool(pool)
    return pool


# ---- Initial state ------------------------------------------------------

def test_initial_state_is_unattached():
    worker = _MinimalWorker()
    assert worker.lifecycle_state is WorkerState.UNATTACHED


def test_default_async_pool_config_is_shared():
    worker = _MinimalWorker()
    assert worker.async_pool_config.mode == "shared"


# ---- TickContext --------------------------------------------------------

def test_tick_context_fields_are_readable():
    ctx = TickContext(tick_id=42, timestamp=1.5, dt=0.02)
    assert ctx.tick_id == 42
    assert ctx.timestamp == 1.5
    assert ctx.dt == 0.02


def test_tick_context_is_immutable():
    ctx = TickContext(tick_id=0, timestamp=0.0, dt=0.0)
    with pytest.raises(Exception):  # frozen dataclass — FrozenInstanceError
        ctx.tick_id = 99  # type: ignore[misc]


# ---- Convenience API: state ---------------------------------------------

def test_declare_and_write_state_under_own_namespace():
    board = WorldBoard()
    worker = _MinimalWorker(name="demo")
    pool = _wire_worker(worker, board)
    try:
        worker.declare_state("demo.x", int)
        worker.write_state("demo.x", 42)
        assert board.read_state("demo.x") == 42
    finally:
        pool.close()


def test_declare_state_outside_own_namespace_raises():
    board = WorldBoard()
    worker = _MinimalWorker(name="demo")
    pool = _wire_worker(worker, board)
    try:
        with pytest.raises(ValueError, match="demo"):
            worker.declare_state("foreign.x", int)
    finally:
        pool.close()


def test_write_state_outside_own_namespace_raises():
    board = WorldBoard()
    worker = _MinimalWorker(name="demo")
    pool = _wire_worker(worker, board)
    try:
        with pytest.raises(ValueError, match="demo"):
            worker.write_state("foreign.x", 1)
    finally:
        pool.close()


def test_read_state_can_cross_namespaces():
    """Reading is unrestricted — any worker can read any state."""
    board = WorldBoard()
    board.declare_state("other.value", int, writer="other")
    board.post_state("other.value", 100, writer="other")

    worker = _MinimalWorker(name="demo")
    pool = _wire_worker(worker, board)
    try:
        assert worker.read_state("other.value") == 100
        assert worker.read_state("nothing", default="x") == "x"
    finally:
        pool.close()


# ---- Convenience API: notes ---------------------------------------------

def test_accept_notes_registers_under_own_namespace():
    board = WorldBoard()
    worker = _MinimalWorker(name="demo")
    pool = _wire_worker(worker, board)
    try:
        received = []
        worker.accept_notes("hello", dict, received.append)

        # Anyone can pass a note addressed to (demo, hello).
        board.pass_note("demo", "hello", {"x": 1}, sender="bt")
        board.deliver_notes()
        assert received == [{"x": 1}]
    finally:
        pool.close()


# ---- Convenience API: run_async ----------------------------------------

def test_run_async_invokes_fn_in_worker_and_on_done_via_drain():
    """Worker pool runs fn immediately; on_done is queued for the next drain."""
    board = WorldBoard()
    worker = _MinimalWorker(name="demo")
    pool = _wire_worker(worker, board)
    try:
        results = []

        def work():
            return 5

        worker.run_async(work, on_done=results.append)

        # Wait for the worker to finish, then drain.
        import time
        time.sleep(0.05)
        pool.drain_main_thread_callbacks()

        assert results == [5]
    finally:
        pool.close()


# ---- Convenience API: misuse without injection -------------------------

def test_declare_state_without_board_raises():
    """Calling subclass APIs before BTClock injection should fail loudly."""
    worker = _MinimalWorker(name="demo")
    with pytest.raises(AssertionError):
        worker.declare_state("demo.x", int)


def test_run_async_without_pool_raises():
    worker = _MinimalWorker(name="demo")
    with pytest.raises(AssertionError):
        worker.run_async(lambda: 1)


# ---- AsyncPoolConfig ----------------------------------------------------

def test_async_pool_config_dedicated_with_workers():
    cfg = AsyncPoolConfig(mode="dedicated", max_workers=3)
    assert cfg.mode == "dedicated"
    assert cfg.max_workers == 3
