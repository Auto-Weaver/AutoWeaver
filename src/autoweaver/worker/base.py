"""Worker base class — see EVO-007."""

from __future__ import annotations

import itertools
import logging
import threading
from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, TypeVar

if TYPE_CHECKING:
    from autoweaver.motion_policy.batch import BatchInfo
    from autoweaver.motion_policy.world_board import WorldBoard
    from autoweaver.worker.async_pool import AsyncPool

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Process-wide monotonic counter used when ``pass_note`` is called without an
# explicit ``request_id``. Starts at 1 so that 0 can safely mean "no request
# observed yet" on the receiver side.
_request_id_counter = itertools.count(1)


def next_request_id() -> int:
    """Allocate the next framework-assigned request_id."""
    return next(_request_id_counter)


@dataclass(frozen=True)
class TickContext:
    """Context passed to ``Worker.on_tick`` once per tick.

    ``tick_id`` is monotonic from clock start; ``timestamp`` is the
    monotonic seconds at which the tick fired; ``dt`` is the actual elapsed
    seconds since the previous tick (lets Workers compensate for
    scheduling jitter).

    All fields are read-only. tick_id is conceptually int64 — at 50 Hz
    overflow takes ~5.85 billion years.
    """

    tick_id: int
    timestamp: float
    dt: float


class WorkerState(Enum):
    """Lifecycle states of a Worker.

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
    """How a Worker wants its async work executed.

    ``mode="shared"`` (default) uses the BTClock's shared worker pool.
    ``mode="dedicated"`` gives the Worker its own pool with the
    requested worker count — for heavy / GPU work that shouldn't compete
    with light IO workers for shared workers.
    """

    mode: str = "shared"  # "shared" | "dedicated"
    max_workers: int = 1


class Worker(ABC):
    """Passive, BT-driven unit responsible for one piece of the outside world.

    A Worker is the unit of "managing one piece of the outside world"
    (a sensor stack, a motion runtime, an IO module, a network adapter).
    It is **not** an active scheduler — it sits idle until BT tree nodes
    pass it notes via ``pass_note``. Subclasses only need to react to
    those notes and to optionally write state asynchronously.

    **Do not subclass `Worker` directly.** The base class only provides
    lifecycle / state / async helpers — it has no note acceptance API.
    Pick a completion-protocol subclass:

      - ``PerceptionWorker`` — handler returns = work done. Suits
        perception, IO, comm, and any case where the work runs inside
        the handler (or via ``run_async``).
      - ``MotionWorker`` — work is started by the handler but completes
        later on a tick-observed state edge. Suits motion control, where
        the real work happens on external hardware over multiple ticks.

    See EVO-007 for the rationale behind the split.

    Lifecycle (driven by BTClock):

        UNATTACHED
          └─ attach_worker() → on_attach() called
        ATTACHED
          └─ on_start() called (may open resources)
        RUNNING ⇄ PAUSED  (pause/resume)
        STOPPED  (on_stop called)
          └─ on_detach() called
        UNATTACHED (or FAULTED if any hook raised, or any note handler raised)

    Required subclass surface:

        name: str                 — globally unique identifier (also namespace)

    Optional hooks (defaults: no-op):

        on_attach()              — declare state, register note acceptors
        on_start()               — open external resources (slow OK here)
        on_pause() / on_resume()
        on_stop()                — close external resources
        on_detach()              — final teardown
        on_tick(ctx)             — periodic work (default no-op for
                                    PerceptionWorker; MotionWorker uses
                                    it as the completion detector)
        on_batch_start(info)     — a new Batch is starting; reset
                                    per-batch internal state

    Subclasses use these convenience methods (do not override):

        declare_state(key, type)
        write_state(key, value)
        read_state(key)
        run_async(fn, on_done)
        run_background(fn, thread_name)

    request_id state fields
    -----------------------
    Every Worker automatically declares three state fields::

        <self.name>.last_request_id     — most recent inbound request_id
        <self.name>.last_completed_id   — last request_id whose work finished
        <self.name>.last_error          — most recent error description

    The fields are declared here for the namespace; **how and when they
    are written** is the responsibility of the completion-protocol
    subclass. BT nodes use these fields via the standard
    ``NotifyAndWait`` pattern.
    """

    # Subclasses may override:
    async_pool_config: AsyncPoolConfig = AsyncPoolConfig(mode="shared")

    def __init__(self) -> None:
        self._lifecycle_state: WorkerState = WorkerState.UNATTACHED
        self._board: WorldBoard | None = None
        self._async_pool: AsyncPool | None = None
        self._background_stop: threading.Event = threading.Event()
        self._background_threads: list[threading.Thread] = []
        # Tracks the request_id currently being handled (used to write
        # last_completed_id after the user handler returns).
        self._current_request_id: int | None = None

    # ------------------------------------------------------------------
    # Subclass contract (required)
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Globally unique identifier; also the Worker's namespace.

        Subclasses must override (either as a class attribute or a
        property). The base implementation raises so that an
        un-overridden Worker fails loudly at attach.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must define a 'name' attribute or property"
        )

    # ------------------------------------------------------------------
    # Subclass contract (optional hooks)
    # ------------------------------------------------------------------

    def on_tick(self, ctx: TickContext) -> None:
        """Default no-op. Most Workers respond to notes and do not need
        per-tick work. Override for periodic concerns: heartbeats,
        timeout sweeps, low-priority polling.

        If override, keep it fast — slow operations go through
        ``self.run_async(...)``. Do not sleep or block on IO here.
        """

    def on_batch_start(self, info: BatchInfo) -> None:
        """Default no-op. A new Batch is starting — reset per-batch state.

        Broadcast by ``BTClock.submit`` to every attached Worker except
        FAULTED ones — **PAUSED Workers are included** — before the Batch
        is attached (EVO-014 §10). Blackboards die with their Batch,
        so they never leak; **Workers do** — a filter's history, a
        tracker's state, a ``Task`` held inside this Worker. This is the
        hook that clears them, and it exists because the knowledge of
        "what is dirty" belongs to the Worker's author, not to the
        business loop.

        There is deliberately no ``on_batch_end``: a killed Batch may
        never reach an end hook, but the next Batch always runs the start
        hook. Wiping the table on the way in beats remembering on the way
        out.

        ``info`` carries only the batch identity (id + ``batch_no``) —
        never ``params``. Those are the tree's; a Worker that needs
        parameters is being *configured*, and configuration arrives at
        ``on_attach``.

        Keep it fast, exactly like ``on_tick`` — slow work goes through
        ``self.run_async(...)``. **If this raises, the Worker goes FAULTED
        and the submit fails**: a Worker that did not reset cleanly would
        make the whole batch's output untrustworthy.
        """

    def on_attach(self) -> None:
        """Called once after framework injection. Do declare_state /
        accept_notes here. Failures mark the worker faulted."""

    def on_start(self) -> None:
        """Open external resources (camera, network, model). May be slow.
        Failures mark the worker faulted; on_stop is still invoked."""

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
        """Declare a state field under this Worker's namespace.

        ``key`` must start with ``self.name + '.'``. Writer is implicitly
        ``self.name``.
        """
        self._require_namespace(key)
        assert self._board is not None
        self._board.declare_state(key, value_type, writer=self.name)

    def write_state(self, key: str, value: Any) -> None:
        """Publish a value to a state field this Worker declared."""
        self._require_namespace(key)
        assert self._board is not None
        self._board.post_state(key, value, writer=self.name)

    def read_state(self, key: str, default: Any = None) -> Any:
        """Read any state field on the WorldBoard (cross-worker read OK)."""
        assert self._board is not None
        return self._board.read_state(key, default)

    def run_async(
        self,
        fn: Callable[[], T],
        on_done: Callable[[T], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """Submit a slow task to the worker pool.

        ``fn`` runs in a worker thread. When it finishes, exactly one
        completion callback is invoked on the **main tick thread** at the
        start of the next tick — ``on_done(result)`` if ``fn`` returned,
        or ``on_error(exception)`` if it raised. Running on the tick
        thread guarantees that anything the callback does (writing state,
        reading state, mutating self) is free of concurrency concerns.

        Pass ``on_error`` whenever the Worker completes a BT request from
        ``on_done`` (e.g. writing ``last_completed_id``): a raised job then
        routes to ``on_error``, which can still complete the request so a
        ``NotifyAndWait`` on it never hangs. If ``on_error`` is omitted and
        ``fn`` raises, the traceback is logged and no callback fires.
        """
        assert self._async_pool is not None
        self._async_pool.submit(fn, on_done, name=self.name, on_error=on_error)

    def run_background(
        self,
        fn: Callable[[threading.Event], None],
        thread_name: str = "",
    ) -> None:
        """Start a long-running daemon thread that runs alongside the tick loop.

        Use for **continuous** background work that doesn't fit the
        ``run_async`` "submit one task, get one callback" model — e.g.
        a comm protocol's polling loop, a sensor's hardware-callback
        bridge, or a watchdog.

        ``fn`` receives one argument: a ``threading.Event`` that the
        framework sets when the Worker is detaching. ``fn`` MUST poll
        ``stop_event.is_set()`` (or ``stop_event.wait(timeout=...)``)
        regularly and return promptly when set; otherwise detach will
        hang.

        Multiple background threads may be started by the same Worker.
        They are all signalled to stop when ``on_stop`` is about to run.

        ``fn`` runs on a daemon thread, so process exit will not block
        on it — but graceful shutdown still relies on the stop_event
        contract.
        """
        if self._lifecycle_state in (
            WorkerState.STOPPED,
            WorkerState.UNATTACHED,
        ):
            raise RuntimeError(
                f"Worker '{self.name}' cannot start background work in "
                f"state {self._lifecycle_state}"
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
                "worker '%s' background thread raised; thread exiting",
                self.name,
            )

    # ------------------------------------------------------------------
    # Framework-only API (called by BTClock; do NOT call from subclasses)
    # ------------------------------------------------------------------

    @property
    def lifecycle_state(self) -> WorkerState:
        return self._lifecycle_state

    def _set_board(self, board: WorldBoard) -> None:
        self._board = board

    def _set_async_pool(self, pool: AsyncPool) -> None:
        self._async_pool = pool

    def _transition(self, new_state: WorkerState) -> None:
        self._lifecycle_state = new_state

    def _declare_framework_state(self) -> None:
        """Called by BTClock right before on_attach.

        Pre-declares the framework-managed state fields under the
        Worker's namespace so that subclasses can read them and the
        framework can safely write them in note handlers.
        """
        assert self._board is not None
        # writer == self.name so that post_state below succeeds.
        self._board.declare_state(
            f"{self.name}.last_request_id", int, writer=self.name
        )
        self._board.declare_state(
            f"{self.name}.last_completed_id", int, writer=self.name
        )
        self._board.declare_state(
            f"{self.name}.last_error", str, writer=self.name
        )
        # Seed sentinels so that BT nodes can read them safely before
        # any note has been received.
        self._board.post_state(
            f"{self.name}.last_request_id", 0, writer=self.name
        )
        self._board.post_state(
            f"{self.name}.last_completed_id", 0, writer=self.name
        )
        self._board.post_state(
            f"{self.name}.last_error", "", writer=self.name
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_namespace(self, key: str) -> None:
        prefix = self.name + "."
        if not key.startswith(prefix):
            raise ValueError(
                f"Worker '{self.name}' may only declare/write state keys "
                f"starting with '{prefix}'; got '{key}'"
            )


def _pop_request_id(payload: Any) -> int | None:
    """Extract a framework-injected request_id from a payload.

    For dict payloads we pop the reserved key ``__request_id__``. For
    non-dict payloads (or dicts without that key) we return None and
    fall back to the no-tracking behavior.

    Shared utility for both PerceptionWorker (synchronous completion)
    and MotionWorker (tick-async completion).
    """
    if isinstance(payload, dict):
        rid = payload.pop("__request_id__", None)
        if isinstance(rid, int):
            return rid
    return None
