"""Tests for Worker.run_background — daemon-thread long-running work."""

from __future__ import annotations

import threading
import time

from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.base import TickContext, Worker, WorkerState
from autoweaver.worker.clock import BTClock


class _BackgroundCounter(Worker):
    def __init__(self, name: str = "bg") -> None:
        super().__init__()
        self._n = name
        self.counter = 0

    @property
    def name(self) -> str:
        return self._n

    def on_start(self) -> None:
        self.run_background(self._loop, thread_name=f"{self._n}-counter")

    def _loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.counter += 1
            stop_event.wait(0.001)


def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"predicate not true within {timeout}s")


def test_run_background_starts_daemon_thread_on_start():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    worker = _BackgroundCounter()
    try:
        clock.attach_worker(worker)
        _wait_for(lambda: worker.counter > 5)
    finally:
        clock.shutdown()


def test_detach_signals_stop_event_and_joins_thread():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    worker = _BackgroundCounter()
    clock.attach_worker(worker)
    _wait_for(lambda: worker.counter > 0)

    threads_before_detach = list(worker._background_threads)
    clock.detach_worker(worker)
    # Background thread must have exited.
    for t in threads_before_detach:
        assert not t.is_alive()


def test_re_attach_after_detach_resets_stop_event():
    """A Worker can be detached and re-attached; the stop event
    must be reset so the next background loop runs again."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    worker = _BackgroundCounter()

    clock.attach_worker(worker)
    _wait_for(lambda: worker.counter > 0)
    snapshot1 = worker.counter
    clock.detach_worker(worker)

    # Re-attach.
    worker.counter = 0
    clock.attach_worker(worker)
    _wait_for(lambda: worker.counter > 0)
    assert worker.counter > 0
    clock.shutdown()
    assert snapshot1 > 0


def test_background_exception_does_not_crash_worker():
    """If the background fn raises, the thread exits but the Worker
    stays attached (other ticks etc. continue)."""
    board = WorldBoard()
    clock = BTClock(world_board=board)

    class _Crashy(Worker):
        @property
        def name(self) -> str:
            return "crash"

        def on_start(self) -> None:
            self.run_background(self._boom)

        def _boom(self, stop_event):
            raise RuntimeError("intentional")

    worker = _Crashy()
    try:
        clock.attach_worker(worker)
        # Tick a few times — Worker must still be RUNNING (not FAULTED)
        # because it was the background thread that died, not on_tick.
        for _ in range(3):
            clock.tick_once()
        assert worker.lifecycle_state is WorkerState.RUNNING
    finally:
        clock.shutdown()
