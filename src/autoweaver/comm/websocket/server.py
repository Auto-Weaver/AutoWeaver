"""WebSocket server transport."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Dict, Optional

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

from ..base import CommSignalBase
from . import DecodeMessage, EncodeMessage, default_decode, default_encode

logger = logging.getLogger(__name__)


class WebSocketServerAdapter(CommSignalBase):
    """Single-client WebSocket server transport.

    The server is NOT started in ``__init__``.  Call ``open()`` to bind
    and start accepting connections — typically done by
    ``CommSideTask.attach()`` so that the transport only runs while the
    engine is active.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        open_timeout: Optional[float] = 10.0,
        ping_interval: Optional[float] = 20.0,
        ping_timeout: Optional[float] = 20.0,
        max_size: int = 1_048_576,
        inbox_size: int = 128,
        decode_message: Optional[DecodeMessage] = None,
        encode_message: Optional[EncodeMessage] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._open_timeout = open_timeout
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._max_size = max_size
        self._decode_message = decode_message or default_decode
        self._encode_message = encode_message or default_encode
        self._incoming: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=inbox_size)
        self._connection_lock = threading.Lock()
        self._current_connection: Optional[ServerConnection] = None
        self._server = None
        self._serve_thread: Optional[threading.Thread] = None

    def open(self) -> None:
        """Bind the server socket and start accepting connections."""
        if self._server is not None:
            return
        self._server = serve(
            self._handle_connection,
            self._host,
            self._port,
            open_timeout=self._open_timeout,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            max_size=self._max_size,
            logger=logger,
        )
        self._serve_thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="websocket-server",
        )
        self._serve_thread.start()
        bound = self._server.socket.getsockname()
        logger.info("WebSocket server listening on %s:%s", bound[0], bound[1])

    def receive(self) -> Optional[Dict[str, Any]]:
        try:
            return self._incoming.get_nowait()
        except queue.Empty:
            return None

    def send(self, message: Dict[str, Any]) -> None:
        payload = self._encode_message(message)
        with self._connection_lock:
            connection = self._current_connection
        if connection is None:
            logger.warning("Send skipped: no client connected")
            return
        try:
            connection.send(payload)
        except ConnectionClosed as exc:
            logger.warning("Send failed (connection closed): %s", exc)
            with self._connection_lock:
                if self._current_connection is connection:
                    self._current_connection = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Send failed: %s", exc)

    def close(self) -> None:
        with self._connection_lock:
            connection = self._current_connection
            self._current_connection = None
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to close client connection: %s", exc)
        if self._server is not None:
            self._server.shutdown()
        if self._serve_thread is not None:
            self._serve_thread.join(timeout=2.0)
            self._serve_thread = None
        self._server = None

    def _handle_connection(self, websocket: ServerConnection) -> None:
        with self._connection_lock:
            self._current_connection = websocket
        logger.info("Client connected: %s", websocket.remote_address)
        try:
            while True:
                raw = websocket.recv()
                message = self._decode_message(raw)
                if message is None:
                    continue
                try:
                    self._incoming.put_nowait(message)
                except queue.Full:
                    logger.warning("Inbox full; dropping message")
        except ConnectionClosed as exc:
            logger.info("Client disconnected: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Client receive failed: %s", exc)
        finally:
            with self._connection_lock:
                if self._current_connection is websocket:
                    self._current_connection = None


__all__ = ["WebSocketServerAdapter"]
