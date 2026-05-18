"""MotionWorker — tick-async completion Worker. See EVO-007.

Handler **starts** the work; completion is observed later as a state
edge on subsequent ticks. Suits motion control: ``move_l(...)`` returns
in milliseconds (the gRPC / SDK call), but the arm physically moves for
seconds, and the only way to know "it's done" is to watch ``busy``
flip back to False.

If you want "handler returns = work done" semantics, use
``PerceptionWorker`` instead.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from autoweaver.worker.base import Worker
from autoweaver.worker.base import _pop_request_id

logger = logging.getLogger(__name__)


class MotionWorker(Worker):
    """Worker whose completion comes from a tick-observed state edge.

    Subclasses provide three things:

      - **dispatch functions** (one per motion note) that simply call
        the underlying driver. Signature ``(payload: dict) -> None``;
        ``__request_id__`` is already popped by the framework, and
        ``last_request_id`` is already written by the time dispatch
        runs. Dispatch does not need to manage pending state or wrap
        the driver call in try/except — the framework handles both.
      - **state reading** in ``on_tick``: read the hardware once,
        ``write_state`` the business fields (done / busy / pose / ...),
        then call exactly one of the edge helpers below.
      - **halt** (optional but recommended): a synchronous note
        registered via ``accept_notes`` (inherited from
        ``PerceptionWorker`` if you mix it in — see below) whose
        handler calls ``self.cancel_pending(reason="halt")`` and then
        the driver's halt primitive.

    Edge helpers (call from ``on_tick``)
    ------------------------------------

      - ``note_busy_started()`` — hardware just transitioned to busy.
        First observation of ``busy==True`` after a dispatch.
      - ``note_completion()`` — hardware just transitioned to idle
        AFTER ``note_busy_started()`` was called. This is the "true
        completion" edge; framework writes ``last_completed_id`` for
        the pending request.
      - ``note_error(msg)`` — hardware reported an error mid-motion.
        Framework records ``last_error``, writes ``last_completed_id``
        (so the BT does not hang), and clears pending state. Does
        **not** transition the Worker to FAULTED — motion errors are
        usually workspace / IK / process issues, not unrecoverable
        worker faults.
      - ``note_idle_tick()`` — optional no-op grace. Call once per tick
        when ``busy`` is False **and** ``note_busy_started()`` has not
        yet been called for the current pending request. After
        ``no_op_tick_threshold`` consecutive idle ticks, the framework
        auto-completes the pending request, treating the motion as a
        "controller skipped because target == current pose" no-op.
        Set ``no_op_tick_threshold = 0`` (the default) to disable.

    Cancellation
    ------------
      - ``cancel_pending(reason)`` — force-complete the current pending
        request without writing ``last_error``. The completed id is
        still written so the BT can move on. Call from a halt handler
        before invoking the driver's halt.

    Note registration
    -----------------
    Use ``accept_async_notes(name, type, dispatch)`` for motion notes.
    This sidesteps the synchronous wrapper that ``PerceptionWorker``
    uses; the framework's async wrapper handles the pending state
    machine, the auto force-complete on overlap, and dispatch
    exception → ``last_error`` recording.

    For synchronous notes on a motion worker (e.g. halt), accept_notes
    is not available on ``MotionWorker`` directly. If a subclass needs
    it, declare halt by direct ``self._board.accept_notes(...)`` and
    handle the request_id manually — or co-inherit with
    ``PerceptionWorker``. The reference implementations
    (``EpsonLS6Worker`` / ``DobotWorker``) take the direct-board route
    for halt; co-inheritance is overkill for one note.
    """

    # Subclasses set > 0 to enable no-op grace. At BT 20Hz, 30 ≈ 1.5s.
    no_op_tick_threshold: int = 0

    def __init__(self) -> None:
        super().__init__()
        # Pending request state machine. Only one motion can be in
        # flight at a time. Dispatching a new motion while one is
        # pending force-completes the old one (with a warning).
        self._pending_request_id: int | None = None
        # Flips True the first time we observe busy=True after dispatch.
        # Until then, a stale done from the previous motion can't
        # falsely complete the pending request.
        self._move_started: bool = False
        # Idle-tick counter for no-op grace.
        self._idle_ticks: int = 0

    # ------------------------------------------------------------------
    # Note registration
    # ------------------------------------------------------------------

    def accept_async_notes(
        self,
        name: str,
        payload_type: type,
        dispatch: Callable[[dict], None],
    ) -> None:
        """Register a tick-async motion note.

        ``dispatch(payload)`` is called from the note delivery phase
        with ``__request_id__`` already popped. By the time it runs,
        the framework has:

          - Written ``last_request_id``
          - Force-completed any previous pending request (with a
            warning log)
          - Recorded the new pending request id

        If ``dispatch`` raises, the framework records ``last_error``,
        force-completes the request, and clears pending state. The
        Worker is **not** transitioned to FAULTED — motion dispatch
        errors are usually transient.
        """
        assert self._board is not None
        wrapped = self._wrap_async_dispatch(dispatch)
        self._board.accept_notes(
            namespace=self.name,
            name=name,
            payload_type=payload_type,
            on_receive=wrapped,
        )

    # ------------------------------------------------------------------
    # Edge helpers (called from on_tick)
    # ------------------------------------------------------------------

    def note_busy_started(self) -> None:
        """Hardware just entered busy=True for the pending request.

        Idempotent within one motion: calling repeatedly after the
        first busy=True observation is a no-op.
        """
        if self._pending_request_id is None:
            return
        if not self._move_started:
            self._move_started = True
            self._idle_ticks = 0

    def note_completion(self) -> None:
        """Hardware just returned to idle for the pending request.

        Only valid after ``note_busy_started`` has fired for this
        request. Calling before busy was seen is a no-op (still waiting
        for the motion to actually start).
        """
        if self._pending_request_id is None:
            return
        if not self._move_started:
            return
        self._write_completion(self._pending_request_id)
        self._clear_pending()

    def note_error(self, msg: str) -> None:
        """Hardware reported an error mid-motion.

        Records ``last_error``, writes ``last_completed_id`` (so the BT
        does not hang waiting for a done that will never come), and
        clears pending state. Does **not** FAULTED the worker.
        """
        if self._pending_request_id is None:
            return
        full_msg = f"motion error during request rid={self._pending_request_id}: {msg}"
        logger.error("MotionWorker '%s': %s", self.name, full_msg)
        try:
            assert self._board is not None
            self._board.post_state(
                f"{self.name}.last_error", full_msg, writer=self.name
            )
        except Exception:
            logger.exception(
                "MotionWorker '%s' failed to record last_error", self.name
            )
        self._write_completion(self._pending_request_id)
        self._clear_pending()

    def note_idle_tick(self) -> None:
        """One tick of "busy is still False" for the pending request.

        If ``no_op_tick_threshold`` is > 0 and we've seen this many
        consecutive idle ticks before ``busy`` ever flipped True, the
        framework auto-completes the request, treating the motion as
        a no-op (controller skipped because target == current pose).

        Subclasses call this once per tick when ``_move_started`` is
        False **and** ``busy`` is False — i.e. before the move has
        ever been observed running.
        """
        if self._pending_request_id is None:
            return
        if self._move_started:
            return  # already running, no-op grace doesn't apply
        if self.no_op_tick_threshold <= 0:
            return
        self._idle_ticks += 1
        if self._idle_ticks >= self.no_op_tick_threshold:
            logger.info(
                "MotionWorker '%s': request_id=%d never entered busy after "
                "%d ticks; treating as no-op completion",
                self.name, self._pending_request_id, self._idle_ticks,
            )
            self._write_completion(self._pending_request_id)
            self._clear_pending()

    def cancel_pending(self, reason: str) -> None:
        """Force-complete the current pending request (e.g. from a halt).

        Writes ``last_completed_id`` so the BT can proceed; does not
        write ``last_error`` (cancellation is not an error). No-op if
        no request is pending.
        """
        if self._pending_request_id is None:
            return
        logger.info(
            "MotionWorker '%s': cancel_pending request_id=%d (reason=%s)",
            self.name, self._pending_request_id, reason,
        )
        self._write_completion(self._pending_request_id)
        self._clear_pending()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _wrap_async_dispatch(
        self, user_dispatch: Callable[[dict], None]
    ) -> Callable[[Any], None]:
        """Wrap the user's dispatch fn with the pending-state machine."""

        def wrapper(payload: Any) -> None:
            request_id = _pop_request_id(payload)
            if request_id is None:
                logger.warning(
                    "MotionWorker '%s': motion note arrived without "
                    "__request_id__ — BT should use NotifyAndWait",
                    self.name,
                )
            else:
                assert self._board is not None
                self._board.post_state(
                    f"{self.name}.last_request_id", int(request_id),
                    writer=self.name,
                )

            # Overlap: a previous motion is still pending. Force-complete
            # the old request so the BT doesn't hang on it. Log loudly —
            # this usually means the BT dispatched two motions without
            # waiting between them.
            if self._pending_request_id is not None:
                logger.warning(
                    "MotionWorker '%s': dispatched new motion while "
                    "request_id=%d still pending; force-completing the old",
                    self.name, self._pending_request_id,
                )
                self._write_completion(self._pending_request_id)
                self._clear_pending()

            try:
                user_dispatch(payload)
            except BaseException as exc:
                logger.exception(
                    "MotionWorker '%s': dispatch raised", self.name,
                )
                try:
                    assert self._board is not None
                    self._board.post_state(
                        f"{self.name}.last_error", repr(exc), writer=self.name,
                    )
                except Exception:
                    logger.exception(
                        "MotionWorker '%s' failed to record last_error",
                        self.name,
                    )
                if request_id is not None:
                    self._write_completion(int(request_id))
                self._clear_pending()
                return

            # Dispatch succeeded; record the new pending request.
            self._pending_request_id = int(request_id) if request_id is not None else None
            self._move_started = False
            self._idle_ticks = 0

        return wrapper

    def _write_completion(self, request_id: int) -> None:
        try:
            assert self._board is not None
            self._board.post_state(
                f"{self.name}.last_completed_id", int(request_id),
                writer=self.name,
            )
        except Exception:
            logger.exception(
                "MotionWorker '%s' failed to record last_completed_id",
                self.name,
            )

    def _clear_pending(self) -> None:
        self._pending_request_id = None
        self._move_started = False
        self._idle_ticks = 0
