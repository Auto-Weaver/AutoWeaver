"""Lightweight state machine for workflow transitions (helper, not a Task)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

from ._event_bus import EventBus


@dataclass(frozen=True)
class Transition:
    trigger: str
    source: Sequence[str]
    dest: str


class StateMachine:
    """Minimal state machine with event-driven transitions."""

    def __init__(
        self,
        initial_state: str,
        transitions: Optional[Iterable[Transition]] = None,
        *,
        name: str = "state_machine",
        event_bus: Optional[EventBus] = None,
        publish_state_changes: bool = True,
    ) -> None:
        self.state = initial_state
        self._transitions: List[Transition] = list(transitions or [])
        self._on_transition: List[Callable[[str, str, str, dict], None]] = []
        self._name = name
        self._event_bus = event_bus
        self._publish_state_changes = publish_state_changes

    def get_state(self) -> str:
        return self.state

    def set_state(self, state: str, *, payload: Optional[dict] = None) -> None:
        if state == self.state:
            return
        old_state = self.state
        self.state = state
        self._notify_transition(old_state, state, "STATE:SET", payload or {})

    def add_transition(self, trigger: str, source: str | Sequence[str], dest: str) -> None:
        sources = (source,) if isinstance(source, str) else tuple(source)
        self._transitions.append(Transition(trigger=trigger, source=sources, dest=dest))

    def on_transition(self, callback: Callable[[str, str, str, dict], None]) -> None:
        """Register callback(old_state, new_state, trigger, payload)."""
        self._on_transition.append(callback)

    def trigger(self, trigger: str, payload: Optional[dict] = None) -> bool:
        data = payload or {}
        for transition in self._transitions:
            if transition.trigger != trigger:
                continue
            if "*" not in transition.source and self.state not in transition.source:
                continue
            old_state = self.state
            self.state = transition.dest
            self._notify_transition(old_state, self.state, trigger, data)
            return True
        return False

    def attach(self, bus: EventBus, events: Optional[Iterable[str]] = None) -> None:
        """Attach to an EventBus and trigger on incoming events."""
        self._event_bus = bus
        if events is None:
            bus.subscribe("*", self._handle_event)
        else:
            for event in events:
                bus.subscribe(event, self._handle_event)

    def _handle_event(self, event: str, payload: dict) -> None:
        self.trigger(event, payload)

    def _notify_transition(self, old_state: str, new_state: str, trigger: str, payload: dict) -> None:
        for cb in self._on_transition:
            cb(old_state, new_state, trigger, payload)
        if self._event_bus and self._publish_state_changes:
            self._event_bus.publish(
                "STATE:CHANGED",
                {
                    "timestamp": time.time(),
                    "source": self._name,
                    "payload": {
                        "old_state": old_state,
                        "new_state": new_state,
                        "trigger": trigger,
                        "data": payload,
                    },
                },
            )
