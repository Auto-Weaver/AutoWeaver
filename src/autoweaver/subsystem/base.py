"""Subsystem base class — see EVO-006."""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, TypeVar

if TYPE_CHECKING:
    from autoweaver.motion_policy.world_board import WorldBoard
    from autoweaver.subsystem.async_pool import AsyncPool

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class TickContext:
    """Context passed to ``Subsystem.on_tick`` once per tick.

    ``tick_id`` is monotonic from clock start; ``timestamp`` is the
    monotonic seconds at which the tick fired; ``dt`` is the actual elapsed
    seconds since the previous tick (lets Subsystems compensate for
    scheduling jitter).

    All fields are read-only. tick_id is conceptually int64 — at 50 Hz
    overflow takes ~5.85 billion years.
    """

    tick_id: int
    timestamp: float
    dt: float


class SubsystemState(Enum):
    """Lifecycle states of a Subsystem.

    Transitions are driven by BTClock; subclasses observe via on_attach /
    on_start / on_stop / on_detach hooks.
    """

    UNATTACHED = "unattached"
    ATTACHED = "attached"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAULTED = "faulted"


@dataclass
class AsyncPoolConfig:
    """How a Subsystem wants its async work executed.

    ``mode="shared"`` (default) uses the BTClock's shared worker pool.
    ``mode="dedicated"`` gives the Subsystem its own pool with the
    requested worker count — for heavy / GPU work that shouldn't compete
    with light IO subsystems for shared workers.
    """

    mode: str = "shared"  # "shared" | "dedicated"
    max_workers: int = 1


class Subsystem(ABC):
    """Tick-driven, passively-responsive subsystem base.

    A Subsystem is the unit of "managing one piece of the outside world"
    (a sensor stack, a motion runtime, an IO module, a network adapter).
    It is woken up by BTClock ticks; it does not maintain its own
    heartbeat.

    Lifecycle (driven by BTClock):

        UNATTACHED
          └─ attach_subsystem() → on_attach() called
        ATTACHED
          └─ on_start() called (may open resources)
        RUNNING ⇄ PAUSED  (pause/resume)
        STOPPED  (on_stop called)
          └─ on_detach() called
        UNATTACHED (or FAULTED if any hook raised)

    Subclasses implement (at minimum):

        name: str                 — globally unique identifier
        on_tick(ctx)              — work for one tick (must be fast)

    Optional hooks: on_attach, on_start, on_pause, on_resume, on_stop,
    on_detach. Default no-ops.

    Subclasses use these convenience methods (do not roll their own):

        declare_state(key, type)  — declare a state field (writer = self.name)
        write_state(key, value)   — publish state value
        read_state(key)           — read any state
        accept_notes(name, payload_type, on_receive)
                                   — receive notes addressed to (self.name, name)
        run_async(fn, on_done)    — submit slow work; on_done fires on the
                                     next tick's main thread

    The Subsystem owns exactly one namespace — by convention, equal to
    ``self.name``. WorldBoard enforces that all state keys begin with
    ``self.name + '.'`` and that notes are addressed to ``self.name``.
    """

    # Subclasses may override:
    async_pool_config: AsyncPoolConfig = AsyncPoolConfig(mode="shared")

    def __init__(self) -> None:
        self._state: SubsystemState = SubsystemState.UNATTACHED
        self._board: WorldBoard | None = None
        self._async_pool: AsyncPool | None = None
        self._background_stop: threading.Event = threading.Event()
        self._background_threads: list[threading.Thread] = []
        # Namespace defaults to self.name — concrete subclasses set name.

    # ------------------------------------------------------------------
    # Subclass contract (required)
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Globally unique identifier; also the Subsystem's namespace
        on the WorldBoard."""

    @abstractmethod
    def on_tick(self, ctx: TickContext) -> None:
        """Called once per tick. Synchronous, fast.

        Slow operations must go through ``self.run_async(...)``. Do not
        sleep or block on IO here.
        """

    # ------------------------------------------------------------------
    # Subclass contract (optional hooks)
    # ------------------------------------------------------------------

    def on_attach(self) -> None:
        """Called once after framework injection. Do declare_state /
        accept_notes here. Failures mark the subsystem faulted."""

    def on_start(self) -> None:
        """Open external resources (camera, network, model). May be slow.
        Failures mark the subsystem faulted; on_stop is still invoked."""

    def on_pause(self) -> None:
        """Stop responding to ticks but keep resources open."""

    def on_resume(self) -> None:
        """Resume tick processing after a pause."""

    def on_stop(self) -> None:
        """Close external resources. Always called even after faults."""

    def on_detach(self) -> None:
        """Final teardown after on_stop."""

    # ------------------------------------------------------------------
    # Convenience API for subclasses (do NOT override)
    # ------------------------------------------------------------------

    def declare_state(self, key: str, value_type: type) -> None:
        """Declare a state field under this Subsystem's namespace.

        ``key`` must start with ``self.name + '.'``. Writer is implicitly
        ``self.name``.
        """
        self._require_namespace(key)
        assert self._board is not None
        self._board.declare_state(key, value_type, writer=self.name)

    def write_state(self, key: str, value: Any) -> None:
        """Publish a value to a state field this Subsystem declared."""
        self._require_namespace(key)
        assert self._board is not None
        self._board.post_state(key, value, writer=self.name)

    def read_state(self, key: str, default: Any = None) -> Any:
        """Read any state field on the WorldBoard (cross-subsystem read OK)."""
        assert self._board is not None
        return self._board.read_state(key, default)

    def accept_notes(
        self,
        name: str,
        payload_type: type,
        on_receive: Callable[[Any], None],
    ) -> None:
        """Declare that this Subsystem will receive notes named ``name``
        (the full address is ``(self.name, name)``)."""
        assert self._board is not None
        self._board.accept_notes(
            namespace=self.name,
            name=name,
            payload_type=payload_type,
            on_receive=on_receive,
        )

    def run_async(
        self,
        fn: Callable[[], T],
        on_done: Callable[[T], None] | None = None,
    ) -> None:
        """Submit a slow task to the worker pool.

        ``fn`` runs in a worker thread. When it completes, ``on_done`` is
        invoked on the **main tick thread** at the start of the next tick.
        This guarantees that anything ``on_done`` does (writing state,
        reading state, mutating self) is free of concurrency concerns.

        If ``fn`` raises, the exception is logged. ``on_done`` only
        receives successful results. To handle errors, wrap ``fn``.
        """
        assert self._async_pool is not None
        self._async_pool.submit(fn, on_done, name=self.name)

    def run_background(
        self,
        fn: Callable[[threading.Event], None],
        thread_name: str = "",
    ) -> None:
        """Start a long-running daemon thread that runs alongside the tick loop.

        Use for **continuous** background work that doesn't fit the
        ``run_async`` "submit one task, get one callback" model — e.g.
        a comm transport's polling loop, a sensor's hardware-callback
        bridge, or a watchdog.

        ``fn`` receives one argument: a ``threading.Event`` that the
        framework sets when the Subsystem is detaching. ``fn`` MUST poll
        ``stop_event.is_set()`` (or ``stop_event.wait(timeout=...)``)
        regularly and return promptly when set; otherwise detach will
        hang.

        Multiple background threads may be started by the same
        Subsystem. They are all signalled to stop when ``on_stop`` is
        about to run.

        ``fn`` runs on a daemon thread, so process exit will not block
        on it — but graceful shutdown still relies on the stop_event
        contract.
        """
        if self._state in (SubsystemState.STOPPED, SubsystemState.UNATTACHED):
            raise RuntimeError(
                f"Subsystem '{self.name}' cannot start background work in "
                f"state {self._state}"
            )
        thread = threading.Thread(
            target=self._background_runner,
            args=(fn,),
            daemon=True,
            name=thread_name or f"{self.name}-bg",
        )
        self._background_threads.append(thread)
        thread.start()

    def _background_runner(
        self, fn: Callable[[threading.Event], None]
    ) -> None:
        try:
            fn(self._background_stop)
        except BaseException:
            logger.exception(
                "subsystem '%s' background thread raised; thread exiting",
                self.name,
            )

    # ------------------------------------------------------------------
    # Framework-only API (called by BTClock; do NOT call from subclasses)
    # ------------------------------------------------------------------

    @property
    def lifecycle_state(self) -> SubsystemState:
        return self._state

    def _set_board(self, board: WorldBoard) -> None:
        self._board = board

    def _set_async_pool(self, pool: AsyncPool) -> None:
        self._async_pool = pool

    def _transition(self, new_state: SubsystemState) -> None:
        self._state = new_state

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_namespace(self, key: str) -> None:
        prefix = self.name + "."
        if not key.startswith(prefix):
            raise ValueError(
                f"Subsystem '{self.name}' may only declare/write state keys "
                f"starting with '{prefix}'; got '{key}'"
            )
