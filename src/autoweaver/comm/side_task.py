"""CommSideTask — base class for communication side-tasks built on TaskBase.

Provides a polling thread that reads from a CommSignalBase transport and
dispatches messages via EventBus. Subclasses override ``subscribe()`` to
register event handlers and ``handle_message()`` to process incoming
transport messages.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from autoweaver.tasks import TaskBase

from .base import CommSignalBase

logger = logging.getLogger(__name__)


class CommSideTask(TaskBase):
    """Base side-task for comm-signal handling.

    Inherits from TaskBase which provides:
    - attach() / subscribe() / broadcast() / close() / _event_bus

    CommSideTask adds:
    - Polling daemon thread for transport I/O
    - send() convenience method
    - handle_message() subclass hook
    """

    name: str = "comm"

    def __init__(
        self,
        transport: CommSignalBase,
        *,
        poll_interval: float = 0.001,
    ) -> None:
        super().__init__()
        self._transport = transport
        self._poll_interval = poll_interval
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False

    # ---- Engine interface overrides ----

    def attach(self, event_bus: Any) -> None:
        """Inject EventBus, subscribe to events, and start polling thread."""
        super().attach(event_bus)  # sets _event_bus and calls subscribe()
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name=f"{self.name}-poll"
        )
        self._poll_thread.start()

    def close(self) -> None:
        """Stop polling thread, close transport, then clean up base."""
        self._running = False
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
        self._transport.close()
        super().close()  # sets _event_bus = None

    # ---- Building blocks ----

    def send(self, message: dict) -> None:
        """Convenience: send a message through the transport."""
        self._transport.send(message)

    def handle_message(self, message: dict) -> Optional[dict]:
        """Subclass hook to process an incoming transport message.

        Return a dict to send a response back through the transport,
        or None to skip responding.
        """
        return None

    # ---- Polling internals ----

    def _poll_loop(self) -> None:
        """Internal polling loop running in a daemon thread."""
        while self._running:
            try:
                self._process_messages()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error in %s poll loop: %s", self.name, exc)
            time.sleep(self._poll_interval)

    def _process_messages(self) -> None:
        """Process all pending transport messages (non-blocking drain)."""
        while self._running:
            message = self._transport.receive()
            if message is None:
                break

            response = self.handle_message(message)
            if response is not None:
                self._transport.send(response)
