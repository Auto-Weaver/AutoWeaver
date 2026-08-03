"""BT Clock — the system's single tick source. See EVO-007."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from autoweaver.motion_policy.batch import BatchState
from autoweaver.worker.async_pool import AsyncPoolRegistry
from autoweaver.worker.base import TickContext, Worker, WorkerState

if TYPE_CHECKING:
    from autoweaver.frames import Frames
    from autoweaver.motion_policy.batch import Batch, BatchResult
    from autoweaver.motion_policy.world_board import WorldBoard

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class BatchHandle:
    """Returned by ``submit``; pass to ``kill`` to stop the Batch early.

    "Waiting for the result" is polling ``state`` / ``result`` from the
    business's own tick loop — the framework offers no blocking wait, on
    purpose (EVO-014 §5: the business owns the loop, policy lives in user
    space).

    ``eq=False`` — a handle is an identity, not a value. ``in`` and
    ``remove`` on the clock's batch list must mean "is it *this* one",
    never "does something with matching fields exist". Harmless while only
    one Batch runs at a time; a landmine the day that limit is lifted.
    """

    name: str
    batch: Batch

    @property
    def state(self) -> BatchState:
        return self.batch.state

    @property
    def result(self) -> BatchResult | None:
        """The exit result, or ``None`` while the Batch is still running."""
        return self.batch.result


class BTClock:
    """The system's single tick source.

    One BTClock per process. It drives a fixed-frequency loop that:

      1. Drains queued worker callbacks (Worker.run_async on_done).
      2. Drains pending notes on the WorldBoard (deliver_notes).
      3. Ticks the running Batch; reaps it once it has EXITED.
      4. Broadcasts on_tick to all RUNNING Workers in attachment order.

    Anything time-sensitive must hook into this loop — Workers may not
    maintain their own heartbeats. Batches and Workers can be submitted
    and attached at runtime.

    The clock offers exactly four verbs for Batches — create (``Batch``),
    submit, get the result, kill — and says nothing about *when* or
    *whether* to run one. That is business policy, and it lives in user
    space (EVO-014 §5).

    Threading:
      - ``run()`` blocks the calling thread; everything in the loop above
        runs on that thread.
      - Worker threads from AsyncPool live on different threads, but
        their on_done callbacks fire here.
      - ``submit`` / ``kill`` / ``attach_*`` / ``detach_*`` / ``stop`` are
        safe to call from any thread (small lock).

    Testing:
      - ``tick_once()`` runs one tick synchronously without sleeping —
        the standard way to drive workers in tests.
    """

    DEFAULT_HZ = 50

    def __init__(
        self,
        world_board: WorldBoard,
        hz: int = DEFAULT_HZ,
        async_pool: AsyncPoolRegistry | None = None,
        frames: Frames | None = None,
    ):
        self._board = world_board
        self._period = 1.0 / hz
        self._async_pool_registry = async_pool or AsyncPoolRegistry()
        self._frames = frames

        # A list, not a single slot. Only one Batch may run at a time, but
        # that limit is a *policy* check in submit() — the structure stays
        # plural so lifting it later is deleting one check, not a refactor
        # (EVO-014 §6).
        self._batches: list[BatchHandle] = []
        self._workers: list[Worker] = []
        self._lock = threading.Lock()
        # Serialises submit() end-to-end so the "one Batch at a time"
        # check cannot race its own append. Kept separate from _lock so
        # user code (Worker.on_batch_start) is never called under _lock.
        self._submit_lock = threading.Lock()

        self._stopped = False
        self._tick_id = 0
        self._last_tick_ts: float | None = None

    # ------------------------------------------------------------------
    # Batch submit / kill
    # ------------------------------------------------------------------

    def submit(self, batch: Batch, name: str | None = None) -> BatchHandle:
        """Submit a Batch. It starts receiving ticks on the next iteration.

        Order:
          1. Reject unless the Batch is READY and no Batch is running.
          2. Inject ``frames=`` (if the clock has one) into both trees.
          3. Broadcast ``on_batch_start`` to every attached Worker except
             FAULTED ones (PAUSED included — see ``_broadcast_batch_start``).
          4. Attach.

        Only **one** Batch may run at a time (EVO-014 §6) — a second
        ``submit`` raises until the running one has EXITED and been
        reaped. A Batch runs once: to run the same work again, build a
        new Batch from the same factory.

        If a Worker's ``on_batch_start`` raises, that Worker goes FAULTED
        and the exception propagates — **the submit fails and the Batch
        does not start** (EVO-014 §10). A Worker that did not reset
        cleanly would poison the batch's results; failing the submit is
        the cheaper outcome.
        """
        if batch.state is not BatchState.READY:
            raise RuntimeError(
                f"Batch '{batch.name}' is {batch.state.value}, cannot submit "
                "(a Batch runs once — build a new one from the same factory)"
            )

        with self._submit_lock:
            with self._lock:
                if self._batches:
                    running = self._batches[0].name
                    raise RuntimeError(
                        f"Batch '{running}' is already running; only one Batch "
                        "may run at a time. Wait for it to EXIT, or kill it."
                    )

            if self._frames is not None:
                batch.set_frames(self._frames)

            self._broadcast_batch_start(batch)

            handle = BatchHandle(name=name or batch.name, batch=batch)
            with self._lock:
                self._batches.append(handle)
            return handle

    def kill(self, handle: BatchHandle) -> None:
        """Begin a Batch's exit: halt its main tree, then run its teardown tree.

        **``kill`` starts the exit; ticking finishes it.** The clock has
        no thread of its own — it only does work inside ``tick_once``. So
        if the Batch has a teardown tree, killing it is not enough::

            clock.kill(handle)
            while handle.result is None:     # ← required, not optional
                clock.tick_once()
                time.sleep(period)
            if not handle.result.teardown_ok:
                ...  # cleanup did not complete — do NOT submit the next one

        Break out of the loop right after ``kill`` and the teardown tree
        is **silently never ticked** — on precisely the path (an abnormal
        stop) that the teardown tree was written for. ``handle.result``
        turning non-None is the only signal that the exit finished.

        With no teardown tree the Batch reaches EXITED immediately and is
        reaped before this returns.

        Idempotent, and safe to call from any thread.
        """
        with self._lock:
            attached = handle in self._batches
        if not attached:
            return
        try:
            handle.batch.kill()
        except Exception:
            logger.exception("batch '%s' kill raised", handle.name)
        self._reap_if_exited(handle)

    def _broadcast_batch_start(self, batch: Batch) -> None:
        """Tell every attached Worker a new Batch is starting.

        Only the minimal batch identity goes out — never ``params``
        (EVO-014 §10). The hook must be fast; slow work belongs in
        ``run_async`` / ``run_background``.

        **PAUSED Workers are included**, unlike ``on_tick``. Skipping a
        PAUSED Worker on ``on_tick`` is correct — paused means "do not
        advance". But ``on_batch_start`` is a *notification*, not a
        tick: "a new batch began, drop your stale state" is true whether
        or not the Worker is currently paused, and a Worker that missed
        it would resume carrying the previous batch's state — the exact
        leak this hook exists to prevent.

        FAULTED Workers are skipped: "broken but still attached" is a
        real state for a Worker (they are drivers, they should not die),
        and a broken one has no business being handed new work.
        """
        with self._lock:
            workers = list(self._workers)
        info = batch.info
        for worker in workers:
            if worker.lifecycle_state is WorkerState.FAULTED:
                continue
            try:
                worker.on_batch_start(info)
            except BaseException:
                logger.exception(
                    "worker '%s' on_batch_start raised; marking FAULTED — "
                    "batch '%s' will not start",
                    worker.name,
                    batch.name,
                )
                worker._transition(WorkerState.FAULTED)
                raise

    def _reap_if_exited(self, handle: BatchHandle) -> None:
        """Detach a Batch that has reached EXITED, freeing the slot."""
        if handle.batch.state is not BatchState.EXITED:
            return
        self._detach(handle)

    def _detach(self, handle: BatchHandle) -> None:
        """Remove a Batch from the clock unconditionally."""
        with self._lock:
            try:
                self._batches.remove(handle)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Worker attach / detach
    # ------------------------------------------------------------------

    def attach_worker(self, worker: Worker) -> None:
        """Attach a Worker to the clock.

        Order:
          1. Inject board + async pool
          2. Pre-declare framework-managed state (last_request_id, ...)
          3. on_attach()  — subclass declares state, accepts notes
          4. on_start()   — subclass opens resources
          5. Mark RUNNING; start receiving ticks on next iteration

        If on_attach or on_start raises, the Worker is marked FAULTED
        and on_stop is called for cleanup. The exception propagates so the
        caller knows attach failed.
        """
        if worker.lifecycle_state is not WorkerState.UNATTACHED:
            raise RuntimeError(
                f"Worker '{worker.name}' is in {worker.lifecycle_state}, "
                "cannot attach (must be UNATTACHED)"
            )

        pool = self._async_pool_registry.make_pool(worker.async_pool_config)
        worker._set_board(self._board)
        worker._set_async_pool(pool)

        try:
            worker._declare_framework_state()
            worker.on_attach()
            worker._transition(WorkerState.ATTACHED)
            worker.on_start()
        except BaseException:
            worker._transition(WorkerState.FAULTED)
            try:
                worker.on_stop()
            except BaseException:
                logger.exception(
                    "worker '%s' on_stop raised during attach failure",
                    worker.name,
                )
            self._async_pool_registry.remove(pool)
            raise

        worker._transition(WorkerState.RUNNING)
        with self._lock:
            self._workers.append(worker)

    def detach_worker(self, worker: Worker) -> None:
        """Detach a Worker.

        Order: stop receiving ticks → signal background threads → on_stop
        → join background threads (best-effort) → on_detach → release pool.
        on_stop and on_detach are best-effort: exceptions are logged but
        don't stop teardown.
        """
        with self._lock:
            try:
                self._workers.remove(worker)
            except ValueError:
                pass

        # Signal any background threads to stop. Subclasses' fn must
        # observe this event; daemon threads will be killed on process
        # exit if they don't, but graceful shutdown depends on the
        # contract.
        worker._background_stop.set()

        try:
            worker.on_stop()
        except BaseException:
            logger.exception(
                "worker '%s' on_stop raised during detach", worker.name
            )

        # Best-effort join with a small timeout — if a background thread
        # ignores stop_event, log and move on.
        for thread in worker._background_threads:
            thread.join(timeout=1.0)
            if thread.is_alive():
                logger.warning(
                    "worker '%s' background thread '%s' did not exit "
                    "within 1s — leaking thread",
                    worker.name, thread.name,
                )
        worker._background_threads.clear()

        worker._transition(WorkerState.STOPPED)
        try:
            worker.on_detach()
        except BaseException:
            logger.exception(
                "worker '%s' on_detach raised during detach", worker.name
            )
        worker._transition(WorkerState.UNATTACHED)
        # Reset the stop event so the Worker can be re-attached.
        worker._background_stop.clear()

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------

    def pause_worker(self, worker: Worker) -> None:
        if worker.lifecycle_state is not WorkerState.RUNNING:
            return
        try:
            worker.on_pause()
        except BaseException:
            logger.exception("worker '%s' on_pause raised", worker.name)
        worker._transition(WorkerState.PAUSED)

    def resume_worker(self, worker: Worker) -> None:
        if worker.lifecycle_state is not WorkerState.PAUSED:
            return
        try:
            worker.on_resume()
        except BaseException:
            logger.exception("worker '%s' on_resume raised", worker.name)
        worker._transition(WorkerState.RUNNING)

    # ------------------------------------------------------------------
    # Tick execution
    # ------------------------------------------------------------------

    def tick_once(self) -> TickContext:
        """Execute one tick synchronously. The standard way to drive the
        system in tests."""
        now = time.monotonic()
        if self._last_tick_ts is None:
            dt = 0.0
        else:
            dt = now - self._last_tick_ts
        self._last_tick_ts = now

        ctx = TickContext(tick_id=self._tick_id, timestamp=now, dt=dt)
        self._tick_id += 1

        # 1. Drain run_async on_done callbacks (main thread).
        try:
            self._async_pool_registry.drain_all()
        except BaseException:
            logger.exception("async pool drain raised; continuing")

        # 2. Deliver pending notes to receivers. Worker.accept_notes wraps
        #    user handlers so that a raising handler transitions its own
        #    Worker to FAULTED and does not propagate out of deliver_notes.
        try:
            self._board.deliver_notes()
        except BaseException:
            logger.exception("note delivery raised; continuing")

        # 3. Tick the running Batch, and reap it if it exited this tick.
        with self._lock:
            batches = list(self._batches)
        for handle in batches:
            try:
                handle.batch.tick(self._board.snapshot(), ctx)
            except BaseException as exc:
                # Unlike a Worker, a broken Batch cannot just be logged
                # and left attached: the Batch slot is exclusive, so
                # "this tree is broken" would silently become "this
                # machine never accepts another batch again". Force the
                # exit and free the slot, whatever it takes.
                logger.exception(
                    "batch '%s' tick raised; forcing it to EXITED", handle.name
                )
                try:
                    handle.batch._abort(exc)
                except BaseException:
                    logger.exception(
                        "batch '%s' abort raised; detaching anyway", handle.name
                    )
                self._detach(handle)
                # KeyboardInterrupt / SystemExit are not errors — Python
                # keeps them off `Exception` precisely so they propagate.
                # The slot is already clean, so re-raising is safe here.
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                continue
            self._reap_if_exited(handle)

        # 4. Broadcast on_tick to RUNNING Workers.
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            if worker.lifecycle_state is not WorkerState.RUNNING:
                continue
            try:
                worker.on_tick(ctx)
            except BaseException:
                logger.exception(
                    "worker '%s' on_tick raised; marking FAULTED", worker.name
                )
                worker._transition(WorkerState.FAULTED)

        return ctx

    def run(self) -> None:
        """Block on the tick loop until ``stop()`` is called."""
        next_deadline = time.monotonic()
        while not self._stopped:
            self.tick_once()
            next_deadline += self._period
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # We're behind schedule; reset the deadline to avoid
                # accumulating drift after a slow tick.
                next_deadline = time.monotonic()

    def stop(self) -> None:
        """Signal ``run()`` to exit at the next loop boundary."""
        self._stopped = True

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def tick_id(self) -> int:
        """Number of ticks fired since clock start."""
        return self._tick_id

    def attached_batches(self) -> list[str]:
        """Names of Batches currently attached (submitted, not yet reaped)."""
        with self._lock:
            return [h.name for h in self._batches]

    def attached_workers(self) -> list[str]:
        with self._lock:
            return [w.name for w in self._workers]

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Detach all workers, kill the running Batch, shut down worker pools.

        Best-effort — exceptions logged, never raised.

        Batches exit through the same path as ``kill``: the main tree is
        halted, so in-flight goals get their ``on_halted`` (this is the
        EVO-010 known-issue #3 that ``detach_tree`` used to skip).

        **shutdown does not tick, so a teardown tree will not run here.**
        Shutting down a clock whose Batch has one halts the main tree,
        logs a warning, and forces the Batch to EXITED anyway — the
        "``result is not None`` ⇔ finished" contract holds even here, so a
        business loop polling on it cannot hang.

        But a result on this path is **not** evidence that the cleanup
        ran — it is evidence of the opposite: ``teardown_outcome`` is
        ``ABORTED``, and ``reason`` keeps whatever the exit path had
        already decided (a kill in flight stays ``KILLED``). If the
        teardown matters (parking the arm, waiting for it to stand still),
        the business must drain it *before* shutting down::

            clock.kill(handle)
            while handle.result is None:
                clock.tick_once()
                time.sleep(period)
            clock.shutdown()

        This is deliberate: draining the teardown inside ``shutdown``
        would mean the framework inventing its own bounded tick loop, and
        the tick loop belongs to the business.
        """
        self._stopped = True

        with self._lock:
            workers = list(self._workers)
            batches = list(self._batches)

        for worker in workers:
            try:
                self.detach_worker(worker)
            except BaseException:
                logger.exception(
                    "shutdown: detach_worker('%s') raised", worker.name
                )

        for handle in batches:
            try:
                self.kill(handle)
            except BaseException:
                logger.exception("shutdown: kill('%s') raised", handle.name)
            if handle.batch.state is not BatchState.EXITED:
                logger.warning(
                    "shutdown: batch '%s' was killed mid-flight but its "
                    "teardown tree cannot run — the clock is shutting down "
                    "and will not tick again",
                    handle.name,
                )
                # Still drive it to EXITED. "result is not None ⇔ finished"
                # is the contract business loops poll on; a Batch stranded
                # short of EXITED by shutdown would hang
                # `while handle.result is None: clock.tick_once()` forever,
                # on the shutdown path of all places.
                try:
                    handle.batch._abort(
                        message="clock shut down before the teardown tree ran"
                    )
                except BaseException:
                    logger.exception(
                        "shutdown: forcing batch '%s' to EXITED raised",
                        handle.name,
                    )
        with self._lock:
            self._batches.clear()

        try:
            self._async_pool_registry.shutdown()
        except BaseException:
            logger.exception("shutdown: async pool registry shutdown raised")
