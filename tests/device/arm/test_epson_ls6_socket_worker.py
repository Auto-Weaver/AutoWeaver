"""Tests for EpsonLS6SocketWorker — verifies it threads notes through
the socket driver under BTClock and publishes state correctly."""

from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from autoweaver.device.arm.epson_ls6.socket_worker import EpsonLS6SocketWorker
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.async_pool import AsyncPool
from autoweaver.worker.base import WorkerState, next_request_id


class _ScriptedServer:
    """Tiny loopback BgMain that answers each line of input with the
    next queued reply. ``queue`` items can be dicts (auto-jsoned) or
    raw strings; replies are sent with CRLF terminator."""

    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.received: list[str] = []
        self._replies: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: socket.socket | None = None

    def queue(self, reply: dict | str) -> None:
        with self._lock:
            self._replies.append(
                json.dumps(reply, separators=(",", ":"))
                if isinstance(reply, dict) else reply
            )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._srv.settimeout(2.0)
            self._conn, _ = self._srv.accept()
            self._conn.settimeout(2.0)
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = self._conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.received.append(line.decode("ascii").strip())
                    with self._lock:
                        reply = self._replies.pop(0) if self._replies else None
                    if reply is not None:
                        self._conn.sendall(reply.encode("ascii") + b"\r\n")
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        try:
            self._srv.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)


@pytest.fixture
def server():
    s = _ScriptedServer()
    s.start()
    yield s
    s.close()


def _wire(worker: EpsonLS6SocketWorker, board: WorldBoard) -> AsyncPool:
    pool = AsyncPool(
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="t"),
        owns_executor=True,
    )
    worker._set_board(board)
    worker._set_async_pool(pool)
    worker._declare_framework_state()
    worker.on_attach()
    return pool


# ----------------------------------------------------------------------------


def test_pick_note_dispatches_and_records_state(server):
    server.queue({"status": "ok", "event": "ready"})  # on_start
    server.queue({"status": "ok", "event": "pick_done"})

    board = WorldBoard()
    worker = EpsonLS6SocketWorker(ip="127.0.0.1", port=server.port, name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_start()
        rid = next_request_id()
        board.pass_note(
            "ls6_1", "pick",
            {"__request_id__": rid, "x": 100.0, "y": 200.0,
             "z": -50.0, "u": 60.0},
            sender="test",
        )
        board.deliver_notes()
        time.sleep(0.05)

        # State recorded
        assert board.read_state("ls6_1.last_x") == 100.0
        assert board.read_state("ls6_1.last_y") == 200.0
        assert board.read_state("ls6_1.last_z") == -50.0
        assert board.read_state("ls6_1.last_u") == 60.0
        assert board.read_state("ls6_1.last_event") == "pick_done"
        # PerceptionWorker wrapper records completion at handler return
        assert board.read_state("ls6_1.last_request_id") == rid
        assert board.read_state("ls6_1.last_completed_id") == rid

        # Server received the start + pick requests
        assert len(server.received) == 2
        assert json.loads(server.received[0]) == {"cmd": "start"}
        assert json.loads(server.received[1]) == {
            "cmd": "pick", "x": 100.0, "y": 200.0, "z": -50.0, "u": 60.0,
        }
    finally:
        worker.on_stop()
        pool.close()


def test_wash_and_task_finish_dispatch(server):
    server.queue({"status": "ok", "event": "ready"})
    server.queue({"status": "ok", "event": "wash_done"})
    server.queue({"status": "ok", "event": "task_finished"})

    board = WorldBoard()
    worker = EpsonLS6SocketWorker(ip="127.0.0.1", port=server.port, name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_start()

        rid_wash = next_request_id()
        board.pass_note(
            "ls6_1", "wash", {"__request_id__": rid_wash}, sender="test",
        )
        board.deliver_notes()
        time.sleep(0.05)
        assert board.read_state("ls6_1.last_event") == "wash_done"
        assert board.read_state("ls6_1.last_completed_id") == rid_wash

        rid_finish = next_request_id()
        board.pass_note(
            "ls6_1", "task_finish",
            {"__request_id__": rid_finish}, sender="test",
        )
        board.deliver_notes()
        time.sleep(0.05)
        assert board.read_state("ls6_1.last_event") == "task_finished"
        assert board.read_state("ls6_1.last_completed_id") == rid_finish
    finally:
        worker.on_stop()
        pool.close()


def test_pick_missing_coord_faults_worker(server):
    server.queue({"status": "ok", "event": "ready"})

    board = WorldBoard()
    worker = EpsonLS6SocketWorker(ip="127.0.0.1", port=server.port, name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_start()

        rid = next_request_id()
        # Missing "u" — KeyError inside handler → PerceptionWorker
        # wrapper records last_error and FAULTS the worker.
        board.pass_note(
            "ls6_1", "pick",
            {"__request_id__": rid, "x": 1.0, "y": 2.0, "z": 3.0},
            sender="test",
        )
        board.deliver_notes()

        assert worker.lifecycle_state is WorkerState.FAULTED
        assert "u" in board.read_state("ls6_1.last_error")
        # Handler raised → completion NOT recorded.
        assert board.read_state("ls6_1.last_completed_id") == 0
    finally:
        worker.on_stop()
        pool.close()


def test_server_error_faults_worker(server):
    server.queue({"status": "ok", "event": "ready"})
    server.queue({"status": "error", "event": "coordinate_invalid"})

    board = WorldBoard()
    worker = EpsonLS6SocketWorker(ip="127.0.0.1", port=server.port, name="ls6_1")
    pool = _wire(worker, board)
    try:
        worker.on_start()

        rid = next_request_id()
        board.pass_note(
            "ls6_1", "pick",
            {"__request_id__": rid, "x": 0.0, "y": 0.0, "z": 0.0, "u": 0.0},
            sender="test",
        )
        board.deliver_notes()
        time.sleep(0.05)

        # Server-side rejection → driver raises EpsonSocketError →
        # PerceptionWorker wrapper FAULTS the worker.
        assert worker.lifecycle_state is WorkerState.FAULTED
        assert "coordinate_invalid" in board.read_state("ls6_1.last_error")
    finally:
        worker.on_stop()
        pool.close()
