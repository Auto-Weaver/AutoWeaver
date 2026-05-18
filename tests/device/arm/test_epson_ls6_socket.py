"""Tests for EpsonLS6Socket TCP/JSON client.

Uses a loopback TCP server to mock SPEL+ BgMain — verifies the wire
protocol (line-delimited JSON, half-duplex, blocking) without
touching real hardware. The reference BgMain implementation lives in
the Epson_Border project (Epson/bgmain.md).
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from autoweaver.device.arm.epson_ls6.socket_driver import (
    EpsonLS6Socket,
    EpsonSocketError,
)


class _MockBgMain:
    """Loopback TCP server that mimics SPEL+ #201 socket behavior.

    Accepts one client, reads newline-terminated JSON lines, and
    replies with whatever's queued via ``queue_reply``. The reply is
    sent with CRLF terminator (matches what SPEL+ ``Print #201``
    actually does on real controllers).
    """

    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.received: list[str] = []
        self._replies: list[str] = []
        self._reply_terminator: bytes = b"\r\n"
        self._thread: threading.Thread | None = None
        self._conn: socket.socket | None = None
        self._stop = threading.Event()

    def queue_reply(self, reply: dict | str) -> None:
        if isinstance(reply, dict):
            self._replies.append(json.dumps(reply, separators=(",", ":")))
        else:
            self._replies.append(reply)

    def set_terminator(self, term: bytes) -> None:
        self._reply_terminator = term

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
                    if self._replies:
                        reply = self._replies.pop(0)
                        self._conn.sendall(reply.encode("ascii")
                                           + self._reply_terminator)
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
def mock_server():
    srv = _MockBgMain()
    srv.start()
    yield srv
    srv.close()


# ---- Connect / disconnect lifecycle ----------------------------------------


def test_connect_is_idempotent(mock_server):
    client = EpsonLS6Socket(ip="127.0.0.1", port=mock_server.port)
    client.connect()
    assert client.is_connected
    client.connect()  # no-op, must not raise
    assert client.is_connected
    client.close()
    assert not client.is_connected


def test_close_is_idempotent(mock_server):
    client = EpsonLS6Socket(ip="127.0.0.1", port=mock_server.port)
    client.connect()
    client.close()
    client.close()  # no-op


# ---- Command roundtrips ----------------------------------------------------


def test_start_handshake_returns_ready(mock_server):
    mock_server.queue_reply({"status": "ok", "event": "ready"})
    client = EpsonLS6Socket(ip="127.0.0.1", port=mock_server.port)
    client.connect()
    try:
        reply = client.send_start()
        # Server got the JSON request
        time.sleep(0.05)
        assert mock_server.received == ['{"cmd":"start"}']
        # Client decoded the JSON reply
        assert reply == {"status": "ok", "event": "ready"}
    finally:
        client.close()


def test_pick_sends_xyzu_payload(mock_server):
    mock_server.queue_reply({"status": "ok", "event": "pick_done"})
    client = EpsonLS6Socket(ip="127.0.0.1", port=mock_server.port)
    client.connect()
    try:
        reply = client.pick(x=113.239, y=251.563, z=-55.838, u=60.244)
        time.sleep(0.05)
        # The serialised request should contain all four coords (we don't
        # pin the key order — json.dumps with separators is deterministic
        # but we don't want to break if the impl swaps to kwargs later).
        sent = json.loads(mock_server.received[0])
        assert sent == {"cmd": "pick", "x": 113.239, "y": 251.563,
                        "z": -55.838, "u": 60.244}
        assert reply["event"] == "pick_done"
    finally:
        client.close()


def test_wash_and_task_finish_send_no_extra_args(mock_server):
    mock_server.queue_reply({"status": "ok", "event": "wash_done"})
    mock_server.queue_reply({"status": "ok", "event": "task_finished"})
    client = EpsonLS6Socket(ip="127.0.0.1", port=mock_server.port)
    client.connect()
    try:
        client.wash()
        client.task_finish()
        time.sleep(0.05)
        assert mock_server.received[0] == '{"cmd":"wash"}'
        assert mock_server.received[1] == '{"cmd":"task_finish"}'
    finally:
        client.close()


# ---- Error handling --------------------------------------------------------


def test_server_error_status_raises(mock_server):
    mock_server.queue_reply({"status": "error", "event": "coordinate_invalid"})
    client = EpsonLS6Socket(ip="127.0.0.1", port=mock_server.port)
    client.connect()
    try:
        with pytest.raises(EpsonSocketError, match="coordinate_invalid"):
            client.pick(x=0.0, y=0.0, z=0.0, u=0.0)
    finally:
        client.close()


def test_send_before_connect_raises():
    client = EpsonLS6Socket(ip="127.0.0.1", port=1)
    with pytest.raises(EpsonSocketError, match="not connected"):
        client.send_start()


def test_malformed_reply_raises(mock_server):
    mock_server.queue_reply("not json at all")
    client = EpsonLS6Socket(ip="127.0.0.1", port=mock_server.port)
    client.connect()
    try:
        with pytest.raises(EpsonSocketError, match="non-JSON"):
            client.send_start()
    finally:
        client.close()


def test_server_disconnect_mid_reply_raises():
    """If the server closes the socket before sending a reply, the client
    raises rather than hanging."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    accepted: list[socket.socket] = []

    def accept_then_close():
        c, _ = srv.accept()
        # Drain the request without replying, then close.
        c.recv(1024)
        accepted.append(c)
        c.close()

    threading.Thread(target=accept_then_close, daemon=True).start()
    client = EpsonLS6Socket(ip="127.0.0.1", port=port, recv_timeout_s=2.0)
    client.connect()
    try:
        with pytest.raises(EpsonSocketError, match="closed"):
            client.send_start()
    finally:
        client.close()
        srv.close()


# ---- Line terminator tolerance ---------------------------------------------


@pytest.mark.parametrize("term", [b"\r\n", b"\n", b"\r"])
def test_accepts_any_line_terminator(term):
    """Real RC+ controllers may emit CRLF, LF, or CR depending on the
    configured Print terminator. Client must tolerate all three."""
    srv = _MockBgMain()
    srv.set_terminator(term)
    srv.start()
    try:
        srv.queue_reply({"status": "ok", "event": "ready"})
        client = EpsonLS6Socket(ip="127.0.0.1", port=srv.port)
        client.connect()
        try:
            reply = client.send_start()
            assert reply == {"status": "ok", "event": "ready"}
        finally:
            client.close()
    finally:
        srv.close()
