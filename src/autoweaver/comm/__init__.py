"""Comm — protocol-driven communication primitives.

Two coexisting lines:

  Message line (CommBase): receive / send / close — for message-shaped peers
    (e.g. WebSocket). ``CommWorker`` wraps it.

  Register line (EVO-009): the declarative-comm engine. ``CommEngine`` runs
    ``write`` / ``read`` / ``read_until`` over a ``RegisterIO`` transport, per
    a ``CommContract``. This is the line the PLC uses — register-level
    handshakes are declared as actions, not coded as a protocol state machine.
"""

from .base import CommBase
from .modbus_primitive import (
    ActionStepError,
    BlockSpec,
    Clock,
    CommActionError,
    CommContract,
    CommEngine,
    ReadUntilTimeout,
    RegisterIO,
)
from .worker import CommWorker

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
    "CommWorker",
    # Register line (EVO-009)
    "CommEngine",
    "CommContract",
    "RegisterIO",
    "BlockSpec",
    "Clock",
    "CommActionError",
    "ReadUntilTimeout",
    "ActionStepError",
    # Message line
    "WebSocketProtocol",
    "WSServerProtocol",
]
