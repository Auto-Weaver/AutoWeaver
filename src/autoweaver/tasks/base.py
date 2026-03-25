"""TaskBase — abstract base class providing building blocks for task implementation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from autoweaver.reactive import EventBus

logger = logging.getLogger(__name__)


class TaskBase:
    """Abstract base class for all tasks.

    Provides building blocks that subclasses use freely:
    - subscribe(): subscribe to EventBus events
    - broadcast(): publish results via EventBus

    Engine calls tick(data) each frame. Subclasses implement tick() to
    call pipeline, do business logic, and broadcast results as needed.
    """

    name: str = ""
    accepts_handoff: bool = False

    def __init__(self) -> None:
        self._event_bus: Optional[EventBus] = None

    # ---- Engine interface ----

    def attach(self, event_bus: EventBus) -> None:
        """Inject EventBus and trigger subscribe()."""
        self._event_bus = event_bus
        self.subscribe()

    def tick(self, data: Any) -> None:
        """Called by Engine each frame. Subclasses override to combine building blocks."""
        raise NotImplementedError

    def reset(self) -> None:
        """Reset stateful components. Subclasses override as needed."""
        pass

    def close(self) -> None:
        """Clean up resources. Subclasses override as needed."""
        self._event_bus = None

    # ---- Building blocks (subclasses override as needed) ----

    def subscribe(self) -> None:
        """Subscribe to EventBus events. Override to add subscriptions."""
        pass

    def broadcast(self, event: str, payload: dict) -> None:
        """Publish result to EventBus."""
        if self._event_bus is not None:
            self._event_bus.publish(event, payload)
        else:
            logger.warning("broadcast() called but no EventBus attached")
