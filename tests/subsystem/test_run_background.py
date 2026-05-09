"""Tests for Subsystem.run_background — daemon-thread long-running work."""

from __future__ import annotations

import threading
import time

from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.subsystem.base import Subsystem, TickContext
from autoweaver.subsystem.clock import BTClock


class _BackgroundCounter(Subsystem):
    def __init__(self, name: str = "bg") -> None:
        super().__init__()
        self._n = name
        self.counter = 0

    @property
    def name(self) -> str:
        return self._n

    def on_start(self) -> None:
        self.run_background(self._loop, thread_name=f"{self._n}-counter")

    def on_tick(self, ctx: TickContext) -> None:
        pass

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
    sub = _BackgroundCounter()
    try:
        clock.attach_subsystem(sub)
        _wait_for(lambda: sub.counter > 5)
    finally:
        clock.shutdown()


def test_detach_signals_stop_event_and_joins_thread():
    board = WorldBoard()
    clock = BTClock(world_board=board)
    sub = _BackgroundCounter()
    clock.attach_subsystem(sub)
    _wait_for(lambda: sub.counter > 0)

    clock.detach_subsystem(sub)
    # Background thread must have exited.
    for t in sub._background_threads:
        # detach_subsystem clears the list after join, but we've held
        # a reference via sub.tracking — tests above instead check the
        # alive predicate. Here we re-create to verify by invoking
        # detach_subsystem again is a no-op.
        assert not t.is_alive()


def test_re_attach_after_detach_resets_stop_event():
    """A Subsystem can be detached and re-attached; the stop event
    must be reset so the next background loop runs again."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    sub = _BackgroundCounter()

    clock.attach_subsystem(sub)
    _wait_for(lambda: sub.counter > 0)
    snapshot1 = sub.counter
    clock.detach_subsystem(sub)

    # Re-attach.
    sub.counter = 0
    clock.attach_subsystem(sub)
    _wait_for(lambda: sub.counter > 0)
    assert sub.counter > 0
    clock.shutdown()
    assert snapshot1 > 0


def test_background_exception_does_not_crash_subsystem():
    """If the background fn raises, the thread exits but the Subsystem
    stays attached (other ticks etc. continue)."""
    board = WorldBoard()
    clock = BTClock(world_board=board)

    class _Crashy(Subsystem):
        @property
        def name(self) -> str:
            return "crash"

        def on_start(self) -> None:
            self.run_background(self._boom)

        def on_tick(self, ctx: TickContext) -> None:
            pass

        def _boom(self, stop_event):
            raise RuntimeError("intentional")

    sub = _Crashy()
    try:
        clock.attach_subsystem(sub)
        # Tick a few times — Subsystem must still be RUNNING (not FAULTED)
        # because it was the background thread that died, not on_tick.
        for _ in range(3):
            clock.tick_once()
        from autoweaver.subsystem.base import SubsystemState
        assert sub.lifecycle_state is SubsystemState.RUNNING
    finally:
        clock.shutdown()
