"""TaskBase — optional helper for EventBus-aware task components.

In 0.6.0+ (EVO-007), Tasks are Worker-internal components, no longer
driven by an Engine.tick(data) loop. ``TaskBase`` is provided as a
small helper for components that need to subscribe / broadcast on an
EventBus the Worker owns. Workers may use it or roll their own.
(Historically these were called Subsystems in 0.5.x.)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from autoweaver.reactive import EventBus

logger = logging.getLogger(__name__)


class TaskBase:
    """Optional helper for tasks that want EventBus integration.

    Subclasses get:
      - ``self._event_bus`` access after ``attach(bus)``
      - ``subscribe()`` hook called once on attach
      - ``broadcast(event, payload)`` helper

    Subclasses define their own work surface — e.g. a method
    ``process(detections)`` invoked by their owning Worker. The
    framework does not impose a fixed entrypoint.
    """

    name: str = ""

    def __init__(self) -> None:
        self._event_bus: Optional[EventBus] = None

    # ---- Lifecycle ----

    def attach(self, event_bus: EventBus) -> None:
        """Inject EventBus and trigger subscribe()."""
        self._event_bus = event_bus
        self.subscribe()

    def reset(self) -> None:
        """Reset stateful components. Subclasses override as needed."""
        pass

    def close(self) -> None:
        """Clean up resources. Subclasses override as needed."""
        self._event_bus = None

    # ---- Building blocks ----

    def subscribe(self) -> None:
        """Subscribe to EventBus events. Override to add subscriptions."""
        pass

    def broadcast(self, event: str, payload: dict) -> None:
        """Publish result to EventBus."""
        if self._event_bus is not None:
            self._event_bus.publish(event, payload)
        else:
            logger.warning("broadcast() called but no EventBus attached")
