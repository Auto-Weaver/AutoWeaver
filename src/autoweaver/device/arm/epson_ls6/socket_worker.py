"""EpsonLS6SocketWorker — Worker around the SPEL+ socket-server driver.

Parallel path to ``EpsonLS6Worker`` (the EtherCAT MotionWorker). When
SPEL+ is running the socket-server flavour (see
``Epson_Border/Epson/bgmain.md``), the arm is driven by short
business-level commands — ``pick(x,y,z,u)`` / ``wash`` /
``task_finish`` — instead of low-level motion primitives. Each command
is a blocking request/response on a TCP socket: the reply only comes
back when SPEL+ has actually finished the motion.

Because the protocol is synchronous (handler returns = motion done),
this Worker inherits ``PerceptionWorker`` rather than ``MotionWorker``:

  - ``accept_notes`` + the framework wrapper give us
    ``last_completed_id`` auto-recorded at handler return — which is
    what we want, because the socket reply already proves completion.
  - There is no tick edge to chase, no busy / done flag to poll, no
    overlap to force-complete.
  - A note handler that raises propagates to the framework's
    FAULTED transition; an exhausted connection is a real fault.

State published under namespace ``<self.name>``:

    <self.name>.last_x       : float — last sent target X (mm, Local 2)
    <self.name>.last_y       : float — last sent target Y
    <self.name>.last_z       : float — last sent target Z
    <self.name>.last_u       : float — last sent target U (deg)
    <self.name>.last_event   : str   — last SPEL+ event ("pick_done", ...)
    <self.name>.last_request_id   : framework-managed
    <self.name>.last_completed_id : framework-managed
    <self.name>.last_error        : framework-managed

Notes accepted (all dict payload):

    "pick"        : {"x": float, "y": float, "z": float, "u": float}
    "wash"        : {}
    "task_finish" : {}
"""

from __future__ import annotations

import logging

from autoweaver.device.arm.epson_ls6.socket_driver import (
    EpsonLS6Socket,
    EpsonSocketError,
)
from autoweaver.worker.perception import PerceptionWorker

logger = logging.getLogger(__name__)


class EpsonLS6SocketWorker(PerceptionWorker):
    """Wraps a single LS6 SPEL+ socket connection as a Worker.

    Connection lifecycle is owned here — ``on_start`` opens the TCP
    socket and sends the required initial ``start`` handshake;
    ``on_stop`` closes it. If anything in there raises, BTClock
    transitions the Worker to FAULTED and BT trees see the namespace
    as dead (downstream WaitFor / NotifyAndWait against this name will
    hang — pair them with ``.timeout()`` decorators if that matters).
    """

    dof = 4

    def __init__(
        self,
        ip: str,
        name: str,
        *,
        port: int = EpsonLS6Socket.DEFAULT_PORT,
        connect_timeout_s: float = 5.0,
        recv_timeout_s: float = 60.0,
    ):
        super().__init__()
        self._name = name
        self._client = EpsonLS6Socket(
            ip=ip,
            port=port,
            connect_timeout_s=connect_timeout_s,
            recv_timeout_s=recv_timeout_s,
        )

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_attach(self) -> None:
        self.declare_state(f"{self._name}.last_x", float)
        self.declare_state(f"{self._name}.last_y", float)
        self.declare_state(f"{self._name}.last_z", float)
        self.declare_state(f"{self._name}.last_u", float)
        self.declare_state(f"{self._name}.last_event", str)
        self.write_state(f"{self._name}.last_event", "")

        self.accept_notes("pick", dict, self._on_pick)
        self.accept_notes("wash", dict, self._on_wash)
        self.accept_notes("task_finish", dict, self._on_task_finish)

    def on_start(self) -> None:
        self._client.connect()
        reply = self._client.send_start()
        logger.info(
            "EpsonLS6SocketWorker '%s' connected, start handshake: %s",
            self._name, reply,
        )
        self.write_state(f"{self._name}.last_event", str(reply.get("event", "")))

    def on_stop(self) -> None:
        try:
            self._client.close()
        except Exception:
            logger.exception(
                "EpsonLS6SocketWorker '%s' close raised", self._name,
            )

    # ------------------------------------------------------------------
    # Note handlers
    # ------------------------------------------------------------------

    def _on_pick(self, payload: dict) -> None:
        # KeyError here is intentional — the framework wrapper records
        # it as last_error and transitions us to FAULTED. Pluck's BT
        # should always supply all four coords.
        x = float(payload["x"])
        y = float(payload["y"])
        z = float(payload["z"])
        u = float(payload["u"])

        reply = self._client.pick(x, y, z, u)
        logger.info(
            "EpsonLS6SocketWorker '%s' pick(%.3f,%.3f,%.3f,%.3f) → %s",
            self._name, x, y, z, u, reply,
        )
        self.write_state(f"{self._name}.last_x", x)
        self.write_state(f"{self._name}.last_y", y)
        self.write_state(f"{self._name}.last_z", z)
        self.write_state(f"{self._name}.last_u", u)
        self.write_state(f"{self._name}.last_event", str(reply.get("event", "")))

    def _on_wash(self, _payload: dict) -> None:
        reply = self._client.wash()
        logger.info("EpsonLS6SocketWorker '%s' wash → %s", self._name, reply)
        self.write_state(f"{self._name}.last_event", str(reply.get("event", "")))

    def _on_task_finish(self, _payload: dict) -> None:
        reply = self._client.task_finish()
        logger.info("EpsonLS6SocketWorker '%s' task_finish → %s", self._name, reply)
        self.write_state(f"{self._name}.last_event", str(reply.get("event", "")))
