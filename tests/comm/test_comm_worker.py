"""Tests for CommWorker — protocol polling integration with the Worker
lifecycle.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

import pytest

from autoweaver.comm.base import CommBase
from autoweaver.comm.worker import CommWorker
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.clock import BTClock


class _FakeProtocol(CommBase):
    """In-memory CommBase used to drive CommWorker in tests."""

    def __init__(self) -> None:
        self._inbox: list[Dict[str, Any]] = []
        self._sent: list[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.closed = False

    # CommBase interface

    def receive(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._inbox:
                return None
            return self._inbox.pop(0)

    def send(self, message: Dict[str, Any]) -> None:
        with self._lock:
            self._sent.append(message)

    def close(self) -> None:
        self.closed = True

    # Test helpers

    def push(self, message: Dict[str, Any]) -> None:
        with self._lock:
            self._inbox.append(message)

    @property
    def sent(self) -> list[Dict[str, Any]]:
        with self._lock:
            return list(self._sent)


class _EchoComm(CommWorker):
    """CommWorker that echoes every inbound message back through the
    protocol, and records what it saw for assertions."""

    def __init__(self, protocol: CommBase, name: str = "echo") -> None:
        super().__init__(protocol, poll_interval=0.001)
        self._name = name
        self.received: list[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self.received.append(message)
        return {"echo": message}


def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"predicate not true within {timeout}s")


def test_attach_starts_polling_and_handles_messages():
    """Once attached, inbound messages are drained on the polling thread."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    protocol = _FakeProtocol()
    worker = _EchoComm(protocol)
    try:
        clock.attach_worker(worker)
        protocol.push({"hello": 1})
        _wait_for(lambda: len(worker.received) >= 1)
        assert worker.received == [{"hello": 1}]
        # Echo response goes back through the protocol.
        _wait_for(lambda: len(protocol.sent) >= 1)
        assert protocol.sent == [{"echo": {"hello": 1}}]
    finally:
        clock.shutdown()


def test_detach_stops_polling_thread_and_closes_protocol():
    """Detach signals the background thread to exit and closes protocol."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    protocol = _FakeProtocol()
    worker = _EchoComm(protocol)
    clock.attach_worker(worker)
    # Confirm thread is alive before detach.
    threads_before_detach = list(worker._background_threads)
    assert any(t.is_alive() for t in threads_before_detach)

    clock.detach_worker(worker)
    # Protocol closed.
    assert protocol.closed is True
    # Background thread joined.
    for t in threads_before_detach:
        assert not t.is_alive()


def test_subsequent_messages_after_detach_are_ignored():
    """After detach, the polling thread is gone — pushed messages stay
    in the protocol's inbox unread."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    protocol = _FakeProtocol()
    worker = _EchoComm(protocol)
    clock.attach_worker(worker)
    clock.detach_worker(worker)

    protocol.push({"after_detach": True})
    time.sleep(0.05)
    assert worker.received == []  # never picked up


def test_handle_message_exception_does_not_crash_polling():
    """A handler exception must not stop the polling loop.

    Note: this is the CommWorker-specific behavior — handle_message
    runs on the polling thread, not the tick main thread, so the
    request_id / FAULTED policy that applies to tick-thread note
    handlers does NOT apply here. Polling-thread exceptions are
    logged and the loop continues.
    """
    board = WorldBoard()
    clock = BTClock(world_board=board)
    protocol = _FakeProtocol()

    received: list[Dict[str, Any]] = []

    class _BoomThenOk(CommWorker):
        def __init__(self, protocol):
            super().__init__(protocol, poll_interval=0.001)

        @property
        def name(self) -> str:
            return "boom"

        def handle_message(self, message):
            received.append(message)
            if message.get("boom"):
                raise RuntimeError("intentional")
            return None

    worker = _BoomThenOk(protocol)
    try:
        clock.attach_worker(worker)
        protocol.push({"boom": True})
        protocol.push({"ok": True})
        _wait_for(lambda: len(received) >= 2)
        assert received == [{"boom": True}, {"ok": True}]
    finally:
        clock.shutdown()


def test_send_works_from_any_thread():
    """Workers can call self.send() at will — the protocol is
    thread-safe (per the test fake)."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    protocol = _FakeProtocol()
    worker = _EchoComm(protocol)
    try:
        clock.attach_worker(worker)
        worker.send({"manual": 1})
        worker.send({"manual": 2})
        _wait_for(lambda: len(protocol.sent) >= 2)
        # Order may include echoes, but our two sends are present.
        manual = [m for m in protocol.sent if m.get("manual") is not None]
        assert len(manual) == 2
    finally:
        clock.shutdown()
