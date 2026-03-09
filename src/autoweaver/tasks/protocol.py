"""Task protocol definition."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from autoweaver.reactive import EventBus


class SideTask(Protocol):
    """Protocol for side tasks that run alongside the main task.

    SideTasks are autonomous: Engine only calls attach() and close().
    Internal execution model (event-driven, polling, hybrid) is the
    SideTask's own concern.
    """

    @property
    def name(self) -> str:
        """Human-friendly task name for logging."""
        ...

    def attach(self, event_bus: EventBus) -> None:
        """Inject EventBus and start internal execution."""
        ...

    def close(self) -> None:
        """Stop internal execution and clean up resources."""
        ...


class Task(Protocol):
    """Protocol for all tasks in the workflow system.

    Tasks satisfy this protocol via structural subtyping (duck typing).
    No inheritance required.
    """

    @property
    def name(self) -> str:
        """Human-friendly task name for logging and metrics."""
        ...

    def attach(self, event_bus: EventBus) -> None:
        """Inject EventBus for event publishing/subscribing."""
        ...

    def run(self, data: Any) -> None:
        """Process a single engine input item (image or handoff data)."""
        ...

    def reset(self) -> None:
        """Reset task state when starting a new session/region."""
        ...

    def close(self) -> None:
        """Clean up resources and unsubscribe from events."""
        ...
