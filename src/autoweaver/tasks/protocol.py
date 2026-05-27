"""Task protocol — minimal contract for Worker-internal task components.

In 0.6.0+ (EVO-007), the Task abstraction is no longer driven by an
Engine. Instead, a Task is a stateful business component held inside a
Worker (e.g. a stabilizer, a tracker, a pick-decision unit). The
Worker orchestrates its tasks however it likes — the Protocol below
is the minimum we ask for so the framework's TaskBase can wire into
optional EventBus subscription if a Worker chooses to use it.

The legacy ``Engine.tick(data)``-driven contract from 0.4.x has been
retired. The ``SideTask`` Protocol and ``RetryCaptureTask`` are gone —
their roles are filled by Worker (the long-lived autonomous
component) and by BT subtrees with Retry decorators (the retry
behaviour) respectively.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from autoweaver.reactive import EventBus


class Task(Protocol):
    """Optional protocol for stateful business components inside a Worker.

    Implementations satisfy this via structural subtyping — no
    inheritance required. Workers may use any shape they like;
    this Protocol is only useful when leaning on the framework's
    ``TaskBase`` helper for EventBus wiring.
    """

    @property
    def name(self) -> str:
        """Human-friendly task name for logging and metrics."""
        ...

    def attach(self, event_bus: EventBus) -> None:
        """Inject EventBus for event publishing/subscribing."""
        ...

    def reset(self) -> None:
        """Reset task state (e.g. when starting a new region/session)."""
        ...

    def close(self) -> None:
        """Clean up resources and unsubscribe from events."""
        ...
