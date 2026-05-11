"""Worker pool for Worker.run_async — see EVO-007.

Slow work runs in worker threads; the on_done callback is queued and
fires on the BTClock's main thread at the start of the next tick. This
keeps state mutation aligned to tick boundaries — workers never have
to think about concurrency in their on_done callbacks.
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class _PendingCallback:
    """A finished worker task, awaiting main-thread invocation."""

    on_done: Callable[[Any], None]
    result: Any
    worker_name: str


class AsyncPool:
    """Worker-facing facade over a thread pool + main-thread callback queue.

    Each Worker holds an ``AsyncPool`` (shared or dedicated). Work
    submitted via ``submit(fn, on_done)`` runs in a worker thread; on
    completion the framework appends ``(on_done, result)`` to
    ``_pending``. BTClock drains ``_pending`` at the start of each
    tick — invoking each callback on the main thread.

    The two phases (worker run, main-thread callback) are decoupled so
    that workers can do GPU/IO work without blocking ticks, while still
    mutating state in a tick-safe way.
    """

    def __init__(self, executor: ThreadPoolExecutor, owns_executor: bool):
        """``owns_executor`` distinguishes shared vs dedicated pools — only
        dedicated pools shut down the executor on close."""
        self._executor = executor
        self._owns_executor = owns_executor
        self._pending: queue.SimpleQueue[_PendingCallback] = queue.SimpleQueue()
        self._closed = False

    def submit(
        self,
        fn: Callable[[], T],
        on_done: Callable[[T], None] | None = None,
        name: str = "",
    ) -> None:
        """Run ``fn`` in a worker thread; queue ``on_done(result)`` for next tick."""
        if self._closed:
            raise RuntimeError("AsyncPool is closed")

        def runner() -> None:
            try:
                result = fn()
            except BaseException:
                logger.exception(
                    "worker '%s' run_async fn raised; on_done suppressed",
                    name,
                )
                return
            if on_done is not None:
                self._pending.put(
                    _PendingCallback(
                        on_done=on_done, result=result, worker_name=name,
                    )
                )

        self._executor.submit(runner)

    def drain_main_thread_callbacks(self) -> None:
        """Invoke all queued on_done callbacks on the calling thread.

        Called by BTClock at the start of every tick. Any callback that
        raises is logged and skipped — one bad on_done cannot starve the
        rest, and cannot crash the tick loop.
        """
        while True:
            try:
                pending = self._pending.get_nowait()
            except queue.Empty:
                return
            try:
                pending.on_done(pending.result)
            except BaseException:
                logger.exception(
                    "worker '%s' on_done callback raised; ignored",
                    pending.worker_name,
                )

    def close(self) -> None:
        """Mark closed; shut down executor if dedicated."""
        self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)


class AsyncPoolRegistry:
    """Owns the shared executor and any dedicated pools.

    BTClock holds one of these. Workers are handed an ``AsyncPool``
    facade pointing at either the shared executor or a freshly-created
    dedicated one based on their ``async_pool_config``.

    All ``AsyncPool`` facades for a clock share the same callback drain
    (each has its own queue, but the registry knows about all of them).
    """

    DEFAULT_SHARED_WORKERS = 4

    def __init__(self, shared_workers: int = DEFAULT_SHARED_WORKERS):
        self._shared_executor = ThreadPoolExecutor(
            max_workers=shared_workers, thread_name_prefix="aw-shared"
        )
        self._pools: list[AsyncPool] = []
        self._lock = threading.Lock()

    def make_pool(self, config) -> AsyncPool:
        """Create an AsyncPool for one Worker according to its config."""
        if config.mode == "shared":
            pool = AsyncPool(self._shared_executor, owns_executor=False)
        elif config.mode == "dedicated":
            executor = ThreadPoolExecutor(
                max_workers=config.max_workers,
                thread_name_prefix="aw-dedicated",
            )
            pool = AsyncPool(executor, owns_executor=True)
        else:
            raise ValueError(
                f"AsyncPoolConfig.mode must be 'shared' or 'dedicated', "
                f"got {config.mode!r}"
            )
        with self._lock:
            self._pools.append(pool)
        return pool

    def drain_all(self) -> None:
        """Drain main-thread callbacks across all pools, in registration order."""
        with self._lock:
            pools = list(self._pools)
        for pool in pools:
            pool.drain_main_thread_callbacks()

    def remove(self, pool: AsyncPool) -> None:
        with self._lock:
            try:
                self._pools.remove(pool)
            except ValueError:
                pass
        pool.close()

    def shutdown(self) -> None:
        with self._lock:
            pools = list(self._pools)
            self._pools.clear()
        for pool in pools:
            pool.close()
        self._shared_executor.shutdown(wait=False, cancel_futures=True)
