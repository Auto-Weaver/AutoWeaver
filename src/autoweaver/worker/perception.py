"""PerceptionWorker — synchronous-completion Worker. See EVO-007.

Handler returns = work done. Suits perception, IO, comm — any case
where the unit of work runs inside the note handler (or via
``run_async`` from inside it).

This is the original ``Worker`` API from 0.6.x, lifted into a dedicated
subclass after 0.8.x split the completion protocol from the lifecycle
base. Code that was previously written against ``Worker`` should switch
to ``PerceptionWorker`` unchanged — the API is the same.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from autoweaver.worker.base import Worker, WorkerState, _pop_request_id

logger = logging.getLogger(__name__)


class PerceptionWorker(Worker):
    """Worker whose note handlers complete synchronously.

    Use this base for any Worker where "the handler returned" is the
    same as "the work is done". Examples:

      - Vision pipelines that read a frame and write a detection state
      - IO modules that respond to a write/read in O(ms)
      - Comm workers translating inbound packets to state writes
      - Anything that uses ``run_async`` and treats the on_done callback
        as the unit of completion (note: see caveat below)

    Completion semantics
    --------------------
    On ``pass_note(self.name, name, payload)`` arrival:

      1. Framework pops ``__request_id__`` from ``payload``
      2. Framework writes ``<self.name>.last_request_id``
      3. Framework calls the user's ``on_receive(payload)``
      4. **If the handler returns**: framework writes
         ``<self.name>.last_completed_id``
      5. **If the handler raises**: framework records ``last_error``
         and transitions the Worker to FAULTED — no more notes are
         delivered until the Worker is detached / reattached.

    Caveat for ``run_async`` users
    ------------------------------
    Step 4 fires at handler return — which is when the *background* task
    was submitted, not when it finished. For perception workers using
    ``run_async``, this is usually fine (BT just observes the result
    state field a few ticks later). If you genuinely need
    ``last_completed_id`` to mark the end of the async work, write it
    yourself from the ``on_done`` callback.
    """

    def accept_notes(
        self,
        name: str,
        payload_type: type,
        on_receive: Callable[[Any], None],
    ) -> None:
        """Declare that this Worker will receive notes named ``name``
        (the full address is ``(self.name, name)``).

        The framework wraps ``on_receive`` to automatically maintain the
        ``last_request_id`` / ``last_completed_id`` protocol and to
        transition the Worker to FAULTED if the handler raises. Subclass
        code receives the payload exactly as passed.
        """
        assert self._board is not None
        wrapped = self._wrap_note_receiver(on_receive)
        self._board.accept_notes(
            namespace=self.name,
            name=name,
            payload_type=payload_type,
            on_receive=wrapped,
        )

    def _wrap_note_receiver(
        self, user_on_receive: Callable[[Any], None]
    ) -> Callable[[Any], None]:
        """Return a receiver that maintains the request_id protocol.

        Pulls the framework-injected ``__request_id__`` out of the
        payload before handing it to the user. If the user handler
        returns successfully, writes ``last_completed_id``. If it raises,
        records ``last_error`` and transitions to FAULTED.
        """

        def wrapper(payload: Any) -> None:
            request_id = _pop_request_id(payload)
            if request_id is not None:
                assert self._board is not None
                self._board.post_state(
                    f"{self.name}.last_request_id", request_id, writer=self.name
                )
                self._current_request_id = request_id
            try:
                user_on_receive(payload)
            except BaseException as exc:
                self._current_request_id = None
                try:
                    assert self._board is not None
                    self._board.post_state(
                        f"{self.name}.last_error", repr(exc), writer=self.name
                    )
                except Exception:
                    logger.exception(
                        "worker '%s' failed to record last_error", self.name
                    )
                self._transition(WorkerState.FAULTED)
                logger.exception(
                    "worker '%s' note handler raised; transitioning to FAULTED",
                    self.name,
                )
                return
            if request_id is not None:
                try:
                    assert self._board is not None
                    self._board.post_state(
                        f"{self.name}.last_completed_id",
                        request_id,
                        writer=self.name,
                    )
                except Exception:
                    logger.exception(
                        "worker '%s' failed to record last_completed_id",
                        self.name,
                    )
            self._current_request_id = None

        return wrapper
