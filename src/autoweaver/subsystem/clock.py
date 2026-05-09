"""BT Clock — the system's single tick source. See EVO-006."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from autoweaver.subsystem.async_pool import AsyncPoolRegistry
from autoweaver.subsystem.base import Subsystem, SubsystemState, TickContext

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

      1. Drains queued worker callbacks (Subsystem.run_async on_done).
      2. Drains pending notes on the WorldBoard (deliver_notes).
      3. Ticks all attached BT trees in attachment order.
      4. Broadcasts on_tick to all RUNNING Subsystems in attachment order.

    Anything time-sensitive must hook into this loop — Subsystems may
    not maintain their own heartbeats. BT trees and Subsystems can be
    attached and detached at runtime.

    Threading:
      - ``run()`` blocks the calling thread; everything in the loop above
        runs on that thread.
      - Worker threads from AsyncPool live on different threads, but
        their on_done callbacks fire here.
      - ``attach_*`` / ``detach_*`` / ``stop`` are safe to call from any
        thread (small lock).

    Testing:
      - ``tick_once()`` runs one tick synchronously without sleeping —
        the standard way to drive subsystems in tests.
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
        self._subsystems: list[Subsystem] = []
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
    # Subsystem attach / detach
    # ------------------------------------------------------------------

    def attach_subsystem(self, sub: Subsystem) -> None:
        """Attach a Subsystem to the clock.

        Order:
          1. Inject board + async pool
          2. on_attach()  — subclass declares state, accepts notes
          3. on_start()   — subclass opens resources
          4. Mark RUNNING; start receiving ticks on next iteration

        If on_attach or on_start raises, the Subsystem is marked FAULTED
        and on_stop is called for cleanup. The exception propagates so the
        caller knows attach failed.
        """
        if sub.lifecycle_state is not SubsystemState.UNATTACHED:
            raise RuntimeError(
                f"Subsystem '{sub.name}' is in {sub.lifecycle_state}, "
                "cannot attach (must be UNATTACHED)"
            )

        pool = self._async_pool_registry.make_pool(sub.async_pool_config)
        sub._set_board(self._board)
        sub._set_async_pool(pool)

        try:
            sub.on_attach()
            sub._transition(SubsystemState.ATTACHED)
            sub.on_start()
        except BaseException:
            sub._transition(SubsystemState.FAULTED)
            try:
                sub.on_stop()
            except BaseException:
                logger.exception(
                    "subsystem '%s' on_stop raised during attach failure",
                    sub.name,
                )
            self._async_pool_registry.remove(pool)
            raise

        sub._transition(SubsystemState.RUNNING)
        with self._lock:
            self._subsystems.append(sub)

    def detach_subsystem(self, sub: Subsystem) -> None:
        """Detach a Subsystem.

        Order: stop receiving ticks → signal background threads → on_stop
        → join background threads (best-effort) → on_detach → release pool.
        on_stop and on_detach are best-effort: exceptions are logged but
        don't stop teardown.
        """
        with self._lock:
            try:
                self._subsystems.remove(sub)
            except ValueError:
                pass

        # Signal any background threads to stop. Subclasses' fn must
        # observe this event; daemon threads will be killed on process
        # exit if they don't, but graceful shutdown depends on the
        # contract.
        sub._background_stop.set()

        try:
            sub.on_stop()
        except BaseException:
            logger.exception("subsystem '%s' on_stop raised during detach", sub.name)

        # Best-effort join with a small timeout — if a background thread
        # ignores stop_event, log and move on.
        for thread in sub._background_threads:
            thread.join(timeout=1.0)
            if thread.is_alive():
                logger.warning(
                    "subsystem '%s' background thread '%s' did not exit "
                    "within 1s — leaking thread",
                    sub.name, thread.name,
                )
        sub._background_threads.clear()

        sub._transition(SubsystemState.STOPPED)
        try:
            sub.on_detach()
        except BaseException:
            logger.exception(
                "subsystem '%s' on_detach raised during detach", sub.name
            )
        sub._transition(SubsystemState.UNATTACHED)
        # Reset the stop event so the Subsystem can be re-attached.
        sub._background_stop.clear()

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------

    def pause_subsystem(self, sub: Subsystem) -> None:
        if sub.lifecycle_state is not SubsystemState.RUNNING:
            return
        try:
            sub.on_pause()
        except BaseException:
            logger.exception("subsystem '%s' on_pause raised", sub.name)
        sub._transition(SubsystemState.PAUSED)

    def resume_subsystem(self, sub: Subsystem) -> None:
        if sub.lifecycle_state is not SubsystemState.PAUSED:
            return
        try:
            sub.on_resume()
        except BaseException:
            logger.exception("subsystem '%s' on_resume raised", sub.name)
        sub._transition(SubsystemState.RUNNING)

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

        # 2. Deliver pending notes to receivers.
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

        # 4. Broadcast on_tick to RUNNING Subsystems.
        with self._lock:
            subs = list(self._subsystems)
        for sub in subs:
            if sub.lifecycle_state is not SubsystemState.RUNNING:
                continue
            try:
                sub.on_tick(ctx)
            except BaseException:
                logger.exception(
                    "subsystem '%s' on_tick raised; marking FAULTED", sub.name
                )
                sub._transition(SubsystemState.FAULTED)

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

    def attached_subsystems(self) -> list[str]:
        with self._lock:
            return [s.name for s in self._subsystems]

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Detach all subsystems and trees, shut down worker pools.

        Best-effort — exceptions logged, never raised.
        """
        self._stopped = True

        with self._lock:
            subs = list(self._subsystems)
            trees = list(self._trees)

        for sub in subs:
            try:
                self.detach_subsystem(sub)
            except BaseException:
                logger.exception(
                    "shutdown: detach_subsystem('%s') raised", sub.name
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
