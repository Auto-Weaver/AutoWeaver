"""EpsonLS6 socket client — TCP / JSON to the SPEL+ BgMain socket server.

Parallel path to the EtherCAT / motion-runtime driver (``driver.py`` /
``worker.py`` in this folder). Use this one when SPEL+ is loaded with
the socket-server flavour of the controller program (see
``Epson_Border/Epson/bgmain.md``) rather than the EtherCAT trigger-
edge flavour.

Why a separate path
-------------------
The EtherCAT trigger-edge protocol has a known race in motion-runtime
0.7.x: the falling-edge piggyback can fire before SPEL+ has had time
to flip ``done`` low and start the motion, so a queued command silently
gets skipped. The socket path sidesteps this entirely — SPEL+'s
``BgMain`` is a blocking request/response loop ("recv → execute
motion → reply"). The reply only comes back when the motion has truly
finished. There is nothing for the master to poll; nothing to race on.

See ``pluck-hair/docs/error/01-ls6-ethercat-trigger-race.md`` for the
full root cause.

Protocol — half-duplex, line-delimited JSON
-------------------------------------------
After TCP connect, the very first message a client sends is
``{"cmd":"start"}``; SPEL+ resets its internal task-timer state and
replies ``{"status":"ok","event":"ready"}``. From there the four
commands available are:

    start         → {"status":"ok", "event":"ready"}
    pick(x,y,z,u) → {"status":"ok", "event":"pick_done"}
    wash          → {"status":"ok", "event":"wash_done"}
    task_finish   → {"status":"ok", "event":"task_finished"}

On error SPEL+ replies ``{"status":"error","event":"<reason>"}`` —
``coordinate_invalid``, ``unknown_cmd``, etc.

The commands are **business-level**, not motion primitives:

  - ``pick`` does (current → safe Z 40 if too low) → Go(x,y,z+30,u) →
    Go(x,y,z,u). i.e. fly to a hover above the target, then descend.
  - ``wash`` does Go(g_tx,g_ty,g_tz+30) → Go(g_tx, wash_y, wash_z, wash_u)
    → Go(g_tx, wash_y, wash_dip_z, wash_u). The wash position's X
    tracks the last pick's X (g_tx is shared global).
  - ``task_finish`` is currently a no-op (just stops the SPEL+ task
    timer); reserved for parking moves the integrator may add later.

Coordinates are in SPEL+ ``Local 2`` frame on the arm side — i.e. the
operator-taught local frame that the bgmain script applies via
``PLocal(P) = 2``. Callers don't need to think about that translation;
just hand over (x, y, z, u) in the same frame the operator uses
during teach.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Optional

logger = logging.getLogger(__name__)


_RECV_CHUNK = 4096


class EpsonSocketError(RuntimeError):
    """Raised on protocol-level failures: malformed reply, server-side
    ``{"status":"error","event":"..."}``, or unexpected disconnect."""


class EpsonLS6Socket:
    """Thin TCP/JSON client to SPEL+ BgMain.

    Threading: one socket per instance, one outstanding command at a
    time. ``send_command`` is internally serialised by a lock — calling
    from multiple Worker threads is safe but they queue. Each command
    blocks until SPEL+ replies (motion finished).

    Lifecycle: construct → ``connect()`` → ``send_start()`` → loop
    ``pick`` / ``wash`` / ``task_finish`` as the BT issues notes →
    ``close()``.

    Timeouts: ``recv_timeout_s`` bounds how long we wait for a reply.
    A motion command can take seconds to many seconds (jump / move),
    so the default is generous (60s). A timeout raises
    ``EpsonSocketError``; the caller decides whether to reconnect.
    """

    DEFAULT_PORT = 1201  # SPEL+ #201 handle bound to TCP 1201 by RC+ config.

    def __init__(
        self,
        ip: str,
        port: int = DEFAULT_PORT,
        *,
        connect_timeout_s: float = 5.0,
        recv_timeout_s: float = 60.0,
    ):
        self._ip = ip
        self._port = port
        self._connect_timeout_s = connect_timeout_s
        self._recv_timeout_s = recv_timeout_s
        self._sock: Optional[socket.socket] = None
        self._recv_buf: bytes = b""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open TCP socket. Idempotent — already-connected is a no-op."""
        with self._lock:
            if self._sock is not None:
                return
            logger.info("EpsonLS6Socket: connecting to %s:%d", self._ip, self._port)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self._connect_timeout_s)
            s.connect((self._ip, self._port))
            s.settimeout(self._recv_timeout_s)
            self._sock = s
            self._recv_buf = b""
            logger.info("EpsonLS6Socket: connected")

    def close(self) -> None:
        """Tear down the socket. Idempotent."""
        with self._lock:
            if self._sock is None:
                return
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._recv_buf = b""
            logger.info("EpsonLS6Socket: disconnected")

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    # ------------------------------------------------------------------
    # High-level commands
    # ------------------------------------------------------------------

    def send_start(self) -> dict:
        """Required handshake right after connect — resets SPEL+ timer
        and confirms the server is responsive."""
        return self._send_and_recv({"cmd": "start"})

    def pick(self, x: float, y: float, z: float, u: float) -> dict:
        """Fly to a hover above (x, y, z+30, u), then descend to
        (x, y, z, u). Blocks until SPEL+ reports done.

        Coordinates are in the operator-taught SPEL+ Local 2 frame.
        """
        return self._send_and_recv(
            {"cmd": "pick", "x": float(x), "y": float(y),
             "z": float(z), "u": float(u)},
        )

    def wash(self) -> dict:
        """Run the SPEL+ wash routine (lift → wash prepare → wash dip).
        Uses the last pick's X coordinate — call after a pick to track
        the workpiece column."""
        return self._send_and_recv({"cmd": "wash"})

    def task_finish(self) -> dict:
        """Stop the SPEL+ task timer; currently no motion."""
        return self._send_and_recv({"cmd": "task_finish"})

    # ------------------------------------------------------------------
    # Internals — line-delimited JSON over TCP
    # ------------------------------------------------------------------

    def _send_and_recv(self, request: dict) -> dict:
        """One request → one reply, under the lock.

        SPEL+ ``Print #201, resp$`` emits the reply with the server's
        configured line terminator (typically CRLF). We accept any of
        ``\\n`` / ``\\r\\n`` / ``\\r`` as a record boundary so we don't
        have to track the controller's exact line discipline.
        """
        payload = json.dumps(request, separators=(",", ":")).encode("ascii")
        with self._lock:
            if self._sock is None:
                raise EpsonSocketError("socket not connected")
            try:
                # SPEL+ ``Read #201, msg$, n`` accepts the raw payload
                # without needing a newline — but we send one anyway so
                # any other tooling (telnet, the reference GUI) reads
                # cleanly.
                self._sock.sendall(payload + b"\n")
                logger.debug("EpsonLS6Socket → %s", request)
                reply_text = self._recv_one_record()
            except (socket.timeout, OSError) as exc:
                raise EpsonSocketError(
                    f"socket I/O failed during {request.get('cmd')!r}: {exc}"
                ) from exc

        try:
            reply = json.loads(reply_text)
        except json.JSONDecodeError as exc:
            raise EpsonSocketError(
                f"non-JSON reply: {reply_text!r}"
            ) from exc

        logger.debug("EpsonLS6Socket ← %s", reply)
        if reply.get("status") == "error":
            raise EpsonSocketError(
                f"SPEL+ rejected {request.get('cmd')!r}: "
                f"event={reply.get('event')!r}"
            )
        return reply

    def _recv_one_record(self) -> str:
        """Pull bytes off the socket until we have a complete line.

        SPEL+ replies are short (a single JSON object), but ``Print``'s
        line terminator may or may not arrive in the same TCP segment
        as the payload. Buffer until we see one and slice it out.
        """
        assert self._sock is not None
        while True:
            for sep in (b"\r\n", b"\n", b"\r"):
                idx = self._recv_buf.find(sep)
                if idx >= 0:
                    line = self._recv_buf[:idx]
                    self._recv_buf = self._recv_buf[idx + len(sep):]
                    return line.decode("ascii", errors="replace").strip()
            chunk = self._sock.recv(_RECV_CHUNK)
            if not chunk:
                raise EpsonSocketError(
                    "socket closed by SPEL+ mid-reply"
                )
            self._recv_buf += chunk
