"""Comm — protocol-driven communication primitives.

Layering:

    CommBase                ← protocol contract (base.py)
    ModbusProtocol / ...    ← concrete protocol mechanics
    CommSubsystem           ← Subsystem template that wraps a protocol

Application code names connections by peer (e.g. ``Nova5Link``,
``PlcLink``) and gives messages business meaning, while picking
which protocol to use.
"""

from .base import CommBase
from .modbus import ModbusProtocol
from .subsystem import CommSubsystem

try:
    from .websocket import WebSocketProtocol, WSServerProtocol
except ModuleNotFoundError as exc:
    if exc.name != "websockets":
        raise
    _WEBSOCKET_IMPORT_ERROR = exc

    class WebSocketProtocol:  # type: ignore[no-redef]
        """Fallback stub when the websocket extra is not installed."""

        def __init__(self, *args, **kwargs) -> None:
            raise ModuleNotFoundError(
                "WebSocketProtocol requires the optional 'websocket' extra. "
                "Install it with `pip install -e \".[websocket]\"`."
            ) from _WEBSOCKET_IMPORT_ERROR

    class WSServerProtocol:  # type: ignore[no-redef]
        """Fallback stub when the websocket extra is not installed."""

        def __init__(self, *args, **kwargs) -> None:
            raise ModuleNotFoundError(
                "WSServerProtocol requires the optional 'websocket' extra. "
                "Install it with `pip install -e \".[websocket]\"`."
            ) from _WEBSOCKET_IMPORT_ERROR

__all__ = [
    "CommBase",
    "CommSubsystem",
    "ModbusProtocol",
    "WebSocketProtocol",
    "WSServerProtocol",
]
