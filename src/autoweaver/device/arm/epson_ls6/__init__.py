"""Epson LS6 SCARA arm — two driver / worker paths.

EtherCAT path (motion-runtime + gRPC, low-level motion primitives):
    EpsonLS6        — ArmBase4 driver, uses RuntimeClient
    EpsonLS6Worker  — MotionWorker subclass

Socket path (SPEL+ BgMain TCP server, business-level commands):
    EpsonLS6Socket        — TCP / JSON client
    EpsonLS6SocketWorker  — PerceptionWorker subclass
                            (synchronous request/response semantics)

The two paths exist because the EtherCAT trigger-edge protocol has a
known race that surfaces under BTClock-driven sequential dispatch;
the socket path sidesteps it via blocking request/response. See
``pluck-hair/docs/error/01-ls6-ethercat-trigger-race.md`` for the
full reasoning. Both paths target the same physical arm — pick one by
loading the matching SPEL+ program on the controller side.
"""

from autoweaver.device.arm.epson_ls6.driver import EpsonLS6
from autoweaver.device.arm.epson_ls6.socket_driver import (
    EpsonLS6Socket,
    EpsonSocketError,
)
from autoweaver.device.arm.epson_ls6.socket_worker import EpsonLS6SocketWorker
from autoweaver.device.arm.epson_ls6.worker import EpsonLS6Worker

__all__ = [
    "EpsonLS6",
    "EpsonLS6Socket",
    "EpsonLS6SocketWorker",
    "EpsonLS6Worker",
    "EpsonSocketError",
]
