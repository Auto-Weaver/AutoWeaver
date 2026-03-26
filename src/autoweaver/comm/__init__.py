from .base import CommSignalBase
from .modbus import ModbusAdapter
from .side_task import CommSideTask

try:
    from .websocket import WebSocketAdapter, WebSocketServerAdapter
except ModuleNotFoundError as exc:
    if exc.name != "websockets":
        raise
    _WEBSOCKET_IMPORT_ERROR = exc

    class WebSocketAdapter:  # type: ignore[no-redef]
        """Fallback stub when the websocket extra is not installed."""

        def __init__(self, *args, **kwargs) -> None:
            raise ModuleNotFoundError(
                "WebSocketAdapter requires the optional 'websocket' extra. "
                "Install it with `pip install -e \".[websocket]\"`."
            ) from _WEBSOCKET_IMPORT_ERROR

    class WebSocketServerAdapter:  # type: ignore[no-redef]
        """Fallback stub when the websocket extra is not installed."""

        def __init__(self, *args, **kwargs) -> None:
            raise ModuleNotFoundError(
                "WebSocketServerAdapter requires the optional 'websocket' extra. "
                "Install it with `pip install -e \".[websocket]\"`."
            ) from _WEBSOCKET_IMPORT_ERROR

__all__ = [
    "CommSignalBase",
    "CommSideTask",
    "ModbusAdapter",
    "WebSocketAdapter",
    "WebSocketServerAdapter",
]
