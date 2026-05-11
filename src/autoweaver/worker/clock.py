"""BT Clock — the system's single tick source. See EVO-007."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from autoweaver.worker.async_pool import AsyncPoolRegistry
from autoweaver.worker.base import TickContext, Worker, WorkerState

if TYPE_CHECKING:
    from autoweaver.motion_policy.action import Action
    from autoweaver.motion_policy.world_board import WorldBoard

logger = logging.getLogger(__name__)


@dataclass
class TreeHandle:
    """Returned by ``attach_tree``; pass to ``detach_tree`` to remove."""

    name: str
    action: Action


class BTClock:
    """The system's single tick source.

    One BTClock per process. It drives a fixed-frequency loop that:

      1. Drains queued worker callbacks (Worker.run_async on_done).
      2. Drains pending notes on the WorldBoard (deliver_notes).
      3. Ticks all attached BT trees in attachment order.
      4. Broadcasts on_tick to all RUNNING Workers in attachment order.

    Anything time-sensitive must hook into this loop — Workers may not
    maintain their own heartbeats. BT trees and Workers can be attached
    and detached at runtime.

    Threading:
      - ``run()`` blocks the calling thread; everything in the loop above
        runs on that thread.
      - Worker threads from AsyncPool live on different threads, but
        their on_done callbacks fire here.
      - ``attach_*`` / ``detach_*`` / ``stop`` are safe to call from any
        thread (small lock).

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
    ):
        self._board = world_board
        self._period = 1.0 / hz
        self._async_pool_registry = async_pool or AsyncPoolRegistry()

        self._trees: list[TreeHandle] = []
        self._workers: list[Worker] = []
        self._lock = threading.Lock()

        self._stopped = False
        self._tick_id = 0
        self._last_tick_ts: float | None = None

    # ------------------------------------------------------------------
    # Tree attach / detach
    # ------------------------------------------------------------------

    def attach_tree(self, action: Action, name: str | None = None) -> TreeHandle:
        """Attach a BT tree (an Action) to the clock.

        The tree starts receiving ticks on the next iteration. The Action
        is responsible for creating its own Blackboard.
        """
        handle = TreeHandle(name=name or action.name, action=action)
        with self._lock:
            self._trees.append(handle)
        return handle

    def detach_tree(self, handle: TreeHandle) -> None:
        """Detach a BT tree. The tree is halted; subsequent ticks skip it."""
        with self._lock:
            try:
                self._trees.remove(handle)
            except ValueError:
                return
        try:
            handle.action.tree.halt()
        except Exception:
            logger.exception("tree '%s' halt raised during detach", handle.name)

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

        # 3. Tick BT trees.
        with self._lock:
            trees = list(self._trees)
        for handle in trees:
            try:
                handle.action.tick(self._board.snapshot())
            except BaseException:
                logger.exception(
                    "tree '%s' tick raised; continuing", handle.name
                )

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

    def attached_trees(self) -> list[str]:
        with self._lock:
            return [h.name for h in self._trees]

    def attached_workers(self) -> list[str]:
        with self._lock:
            return [w.name for w in self._workers]

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Detach all workers and trees, shut down worker pools.

        Best-effort — exceptions logged, never raised.
        """
        self._stopped = True

        with self._lock:
            workers = list(self._workers)
            trees = list(self._trees)

        for worker in workers:
            try:
                self.detach_worker(worker)
            except BaseException:
                logger.exception(
                    "shutdown: detach_worker('%s') raised", worker.name
                )

        for handle in trees:
            try:
                self.detach_tree(handle)
            except BaseException:
                logger.exception(
                    "shutdown: detach_tree('%s') raised", handle.name
                )

        try:
            self._async_pool_registry.shutdown()
        except BaseException:
            logger.exception("shutdown: async pool registry shutdown raised")
