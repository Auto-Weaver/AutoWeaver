"""CommWorker — Worker base for protocol-driven communication.

Wraps a ``CommBase`` protocol with the standard Worker lifecycle. The
protocol's polling loop runs as a background thread
(``run_background``); incoming messages are handled on the worker
thread by ``handle_message``, while workers that need tick-aligned
state writes can hand off via ``run_async`` or accept_notes.

This replaces the legacy ``CommSideTask`` (0.4.x), which relied on the
retired ``SideTask`` protocol and EventBus.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from autoweaver.comm.base import CommBase
from autoweaver.worker.base import TickContext, Worker

logger = logging.getLogger(__name__)


class CommWorker(Worker):
    """Base Worker for protocol-driven I/O.

    Subclasses provide the protocol (via constructor) and override
    ``handle_message`` to react to inbound messages. The framework runs
    a daemon polling thread that drains the protocol until detach.

    Outbound: subclasses (or anyone holding a reference) call
    ``self.send(message)``.

    Subclasses that need to publish state from a poll-thread message
    should hand off via ``self.run_async`` or by passing a note to
    themselves — direct ``self.write_state`` from the polling thread is
    technically allowed (WorldBoard is thread-safe) but breaks the
    "tick is the only state-mutation window" invariant.
    """

    def __init__(
        self,
        protocol: CommBase,
        *,
        poll_interval: float = 0.001,
    ) -> None:
        super().__init__()
        self._protocol = protocol
        self._poll_interval = poll_interval

    # ------------------------------------------------------------------
    # Subclass override
    # ------------------------------------------------------------------

    def handle_message(self, message: dict) -> Optional[dict]:
        """Process an incoming protocol message.

        Return a dict to send a response, or None to skip. Default no-op.

        Runs on the polling thread. To mutate Worker state safely on
        the tick thread, dispatch via ``self.run_async`` or pass a note
        to yourself.
        """
        return None

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def send(self, message: dict) -> None:
        """Send a message through the protocol (any thread)."""
        self._protocol.send(message)

    # ------------------------------------------------------------------
    # Worker lifecycle integration
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Start the polling background thread.

        Subclasses that override should call ``super().on_start()``.
        """
        super().on_start()
        self.run_background(self._poll_loop, thread_name=f"{self.name}-poll")

    def on_stop(self) -> None:
        """Close the protocol. Background thread is signalled to stop
        by the framework before this runs."""
        try:
            self._protocol.close()
        except Exception:
            logger.exception(
                "worker '%s' protocol close raised", self.name
            )
        super().on_stop()

    def on_tick(self, ctx: TickContext) -> None:
        """Default no-op — comm workers are usually driven by the
        polling thread, not the tick. Subclasses may override to do
        periodic tick-aligned work (heartbeats, timeout sweeps, etc.).
        """

    # ------------------------------------------------------------------
    # Polling internals
    # ------------------------------------------------------------------

    def _poll_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self._drain_messages()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "worker '%s' poll loop error: %s", self.name, exc
                )
            # Use stop_event.wait so we exit promptly when set.
            stop_event.wait(self._poll_interval)

    def _drain_messages(self) -> None:
        """Drain all pending protocol messages without blocking."""
        while True:
            message = self._protocol.receive()
            if message is None:
                break
            try:
                response = self.handle_message(message)
            except Exception:
                logger.exception(
                    "worker '%s' handle_message raised", self.name
                )
                continue
            if response is not None:
                try:
                    self._protocol.send(response)
                except Exception:
                    logger.exception(
                        "worker '%s' response send raised", self.name
                    )
