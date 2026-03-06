"""Synchronous event bus for in-process pub/sub (helper, not a Task)."""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict], None]


class EventBus:
    """Simple synchronous publish/subscribe bus."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[EventHandler]] = {}

    def subscribe(self, event: str, handler: EventHandler) -> Callable[[], None]:
        """Subscribe to an event. Use '*' to receive all events."""
        self._subscribers.setdefault(event, []).append(handler)

        def _unsubscribe() -> None:
            self.unsubscribe(event, handler)

        return _unsubscribe

    def unsubscribe(self, event: str, handler: EventHandler) -> None:
        handlers = self._subscribers.get(event)
        if not handlers:
            return
        self._subscribers[event] = [h for h in handlers if h is not handler]
        if not self._subscribers[event]:
            self._subscribers.pop(event, None)

    def publish(self, event: str, payload: Optional[dict] = None) -> None:
        """Publish an event synchronously to all subscribers."""
        data = payload or {}
        for key in (event, "*"):
            for handler in list(self._subscribers.get(key, [])):
                try:
                    handler(event, data)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Event handler failed for %s: %s", key, exc)
