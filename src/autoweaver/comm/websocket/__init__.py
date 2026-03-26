"""WebSocket transport adapters for AutoWeaver.

Shared codec and type aliases live here; client and server
implementations are in their own modules.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

RawMessage = str | bytes
DecodeMessage = Callable[[RawMessage], Optional[Dict[str, Any]]]
EncodeMessage = Callable[[Dict[str, Any]], RawMessage]


def default_decode(raw: RawMessage) -> Optional[Dict[str, Any]]:
    """Decode one incoming JSON object."""
    decoded = json.loads(raw)
    if decoded is None:
        return None
    if not isinstance(decoded, dict):
        raise ValueError("WebSocket message must decode to a JSON object")
    return decoded


def default_encode(message: Dict[str, Any]) -> RawMessage:
    """Encode one outgoing JSON object."""
    return json.dumps(message)


from .client import WebSocketAdapter  # noqa: E402
from .server import WebSocketServerAdapter  # noqa: E402

__all__ = ["WebSocketAdapter", "WebSocketServerAdapter"]
