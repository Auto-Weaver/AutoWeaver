"""WebSocket-based comm signal adapter."""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any, Callable, Dict, Optional, Sequence

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.sync.client import connect

from .base import CommSignalBase

logger = logging.getLogger(__name__)

RawMessage = str | bytes
DecodeMessage = Callable[[RawMessage], Optional[Dict[str, Any]]]
EncodeMessage = Callable[[Dict[str, Any]], RawMessage]


def _default_decode_message(raw: RawMessage) -> Optional[Dict[str, Any]]:
    """Decode a JSON object message from a WebSocket frame."""
    decoded = json.loads(raw)
    if decoded is None:
        return None
    if not isinstance(decoded, dict):
        raise ValueError("WebSocket message must decode to a JSON object")
    return decoded


def _default_encode_message(message: Dict[str, Any]) -> RawMessage:
    """Encode a message dict into a JSON text frame."""
    return json.dumps(message)


class WebSocketAdapter(CommSignalBase):
    """WebSocket client transport for comm messages.

    The adapter maintains a background receiver thread so ``receive()``
    remains non-blocking and fits the existing ``CommSideTask`` polling model.
    By default, frames are encoded as JSON objects.
    """

    def __init__(
        self,
        uri: str,
        *,
        timeout: Optional[float] = 10.0,
        receive_timeout: float = 0.05,
        close_timeout: float = 10.0,
        ping_interval: Optional[float] = 20.0,
        ping_timeout: Optional[float] = 20.0,
        inbox_size: int = 128,
        additional_headers: Optional[Dict[str, str]] = None,
        subprotocols: Optional[Sequence[str]] = None,
        decode_message: Optional[DecodeMessage] = None,
        encode_message: Optional[EncodeMessage] = None,
    ) -> None:
        self.uri = uri
        self._receive_timeout = receive_timeout
        self._decode_message = decode_message or _default_decode_message
        self._encode_message = encode_message or _default_encode_message
        self._incoming: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=inbox_size)
        self._send_lock = threading.Lock()
        self._running = threading.Event()
        self._closed = threading.Event()

        try:
            self._connection = connect(
                self.uri,
                timeout=timeout,
                close_timeout=close_timeout,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
                additional_headers=additional_headers,
                subprotocols=subprotocols,
            )
        except (InvalidURI, InvalidHandshake, OSError, TimeoutError) as exc:
            raise ConnectionError(f"Failed to connect WebSocket {self.uri}") from exc

        self._running.set()
        self._recv_thread = threading.Thread(
            target=self._recv_loop,
            daemon=True,
            name="websocket-recv",
        )
        self._recv_thread.start()

    def receive(self) -> Optional[Dict[str, Any]]:
        """Receive the next decoded message without blocking."""
        try:
            return self._incoming.get_nowait()
        except queue.Empty:
            return None

    def send(self, message: Dict[str, Any]) -> None:
        """Send a message dict as a WebSocket frame."""
        if not self._running.is_set():
            logger.warning("WebSocket send skipped because the connection is closed")
            return

        payload = self._encode_message(message)
        try:
            with self._send_lock:
                self._connection.send(payload)
        except ConnectionClosed as exc:
            logger.warning("WebSocket send failed because the connection is closed: %s", exc)
            self._running.clear()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WebSocket send failed: %s", exc)

    def close(self) -> None:
        """Stop receiver thread and close the WebSocket connection."""
        if self._closed.is_set():
            return

        self._closed.set()
        self._running.clear()

        try:
            with self._send_lock:
                self._connection.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to close WebSocket connection: %s", exc)

        self._recv_thread.join(timeout=max(1.0, self._receive_timeout * 4))

    def _recv_loop(self) -> None:
        """Read frames in the background and enqueue decoded messages."""
        while self._running.is_set():
            try:
                raw = self._connection.recv(timeout=self._receive_timeout)
            except TimeoutError:
                continue
            except ConnectionClosed as exc:
                logger.info("WebSocket connection closed: %s", exc)
                self._running.clear()
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebSocket receive failed: %s", exc)
                self._running.clear()
                break

            try:
                message = self._decode_message(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebSocket decode failed: %s", exc)
                continue

            if message is None:
                continue

            try:
                self._incoming.put_nowait(message)
            except queue.Full:
                logger.warning("WebSocket inbox is full; dropping message")


__all__ = ["WebSocketAdapter"]
