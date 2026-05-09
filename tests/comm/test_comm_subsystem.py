"""Tests for CommSubsystem — transport polling integration with the Subsystem
lifecycle.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

import pytest

from autoweaver.comm.base import CommSignalBase
from autoweaver.comm.subsystem import CommSubsystem
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.subsystem.clock import BTClock


class _FakeTransport(CommSignalBase):
    """In-memory CommSignalBase used to drive CommSubsystem in tests."""

    def __init__(self) -> None:
        self._inbox: list[Dict[str, Any]] = []
        self._sent: list[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.closed = False

    # CommSignalBase interface

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


class _EchoComm(CommSubsystem):
    """CommSubsystem that echoes every inbound message back through the
    transport, and records what it saw for assertions."""

    def __init__(self, transport: CommSignalBase, name: str = "echo") -> None:
        super().__init__(transport, poll_interval=0.001)
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
    transport = _FakeTransport()
    sub = _EchoComm(transport)
    try:
        clock.attach_subsystem(sub)
        transport.push({"hello": 1})
        _wait_for(lambda: len(sub.received) >= 1)
        assert sub.received == [{"hello": 1}]
        # Echo response goes back through the transport.
        _wait_for(lambda: len(transport.sent) >= 1)
        assert transport.sent == [{"echo": {"hello": 1}}]
    finally:
        clock.shutdown()


def test_detach_stops_polling_thread_and_closes_transport():
    """Detach signals the background thread to exit and closes transport."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    transport = _FakeTransport()
    sub = _EchoComm(transport)
    clock.attach_subsystem(sub)
    # Confirm thread is alive before detach.
    assert any(t.is_alive() for t in sub._background_threads)

    clock.detach_subsystem(sub)
    # Transport closed.
    assert transport.closed is True
    # Background thread joined.
    for t in sub._background_threads:
        assert not t.is_alive()


def test_subsequent_messages_after_detach_are_ignored():
    """After detach, the polling thread is gone — pushed messages stay
    in the transport's inbox unread."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    transport = _FakeTransport()
    sub = _EchoComm(transport)
    clock.attach_subsystem(sub)
    clock.detach_subsystem(sub)

    transport.push({"after_detach": True})
    time.sleep(0.05)
    assert sub.received == []  # never picked up


def test_handle_message_exception_does_not_crash_polling():
    """A handler exception must not stop the polling loop."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    transport = _FakeTransport()

    received: list[Dict[str, Any]] = []

    class _BoomThenOk(CommSubsystem):
        def __init__(self, transport):
            super().__init__(transport, poll_interval=0.001)

        @property
        def name(self) -> str:
            return "boom"

        def handle_message(self, message):
            received.append(message)
            if message.get("boom"):
                raise RuntimeError("intentional")
            return None

    sub = _BoomThenOk(transport)
    try:
        clock.attach_subsystem(sub)
        transport.push({"boom": True})
        transport.push({"ok": True})
        _wait_for(lambda: len(received) >= 2)
        assert received == [{"boom": True}, {"ok": True}]
    finally:
        clock.shutdown()


def test_send_works_from_any_thread():
    """Subsystems can call self.send() at will — the transport is
    thread-safe (per the test fake)."""
    board = WorldBoard()
    clock = BTClock(world_board=board)
    transport = _FakeTransport()
    sub = _EchoComm(transport)
    try:
        clock.attach_subsystem(sub)
        sub.send({"manual": 1})
        sub.send({"manual": 2})
        _wait_for(lambda: len(transport.sent) >= 2)
        # Order may include echoes, but our two sends are present.
        manual = [m for m in transport.sent if m.get("manual") is not None]
        assert len(manual) == 2
    finally:
        clock.shutdown()
