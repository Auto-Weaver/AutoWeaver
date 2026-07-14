"""Tests for AsyncPool — worker submission and main-thread on_done drain."""

from __future__ import annotations

import threading
import time

import pytest

from autoweaver.worker.async_pool import AsyncPool, AsyncPoolRegistry
from autoweaver.worker.base import AsyncPoolConfig


# ---- Helpers ------------------------------------------------------------

def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.005) -> None:
    """Poll until predicate() is truthy, or fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"predicate not true within {timeout}s")


# ---- AsyncPool: shared mode --------------------------------------------

def test_submit_runs_fn_and_queues_on_done_for_drain():
    registry = AsyncPoolRegistry(shared_workers=2)
    try:
        pool = registry.make_pool(AsyncPoolConfig())  # shared default
        results = []

        pool.submit(lambda: 42, on_done=results.append)
        # Wait for the worker to finish.
        _wait_for(lambda: not pool._pending.empty())

        # on_done has NOT fired yet (only drain runs callbacks on this thread).
        assert results == []
        pool.drain_main_thread_callbacks()
        assert results == [42]
    finally:
        registry.shutdown()


def test_drain_runs_callbacks_in_completion_order():
    """If 3 jobs finish, drain runs their on_done in queue order."""
    registry = AsyncPoolRegistry(shared_workers=1)  # serialize work
    try:
        pool = registry.make_pool(AsyncPoolConfig())
        seen: list[int] = []
        # Single worker ⇒ jobs serialize ⇒ order matches submission.
        pool.submit(lambda: 1, on_done=seen.append)
        pool.submit(lambda: 2, on_done=seen.append)
        pool.submit(lambda: 3, on_done=seen.append)
        _wait_for(lambda: pool._pending.qsize() == 3)
        pool.drain_main_thread_callbacks()
        assert seen == [1, 2, 3]
    finally:
        registry.shutdown()


def test_on_done_runs_on_the_drain_caller_thread():
    """Callbacks must execute on the thread that called drain — that's
    the BTClock's main thread in production."""
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool = registry.make_pool(AsyncPoolConfig())
        observed_thread: list[int] = []
        pool.submit(
            lambda: None,
            on_done=lambda _: observed_thread.append(threading.get_ident()),
        )
        _wait_for(lambda: not pool._pending.empty())
        my_tid = threading.get_ident()
        pool.drain_main_thread_callbacks()
        assert observed_thread == [my_tid]
    finally:
        registry.shutdown()


def test_fn_exception_without_on_error_skips_callbacks():
    """If fn raises and no on_error is given, on_done must not run, and the
    pool keeps working. (No callback queued for the failed job.)"""
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool = registry.make_pool(AsyncPoolConfig())
        results: list = []

        def bad():
            raise RuntimeError("boom")

        pool.submit(bad, on_done=results.append)
        # Submit a successful one after — pool must still process it.
        pool.submit(lambda: "ok", on_done=results.append)
        _wait_for(lambda: pool._pending.qsize() == 1)
        pool.drain_main_thread_callbacks()
        assert results == ["ok"]
    finally:
        registry.shutdown()


def test_fn_exception_routes_to_on_error():
    """If fn raises and on_error is given, on_error receives the exception
    (drained on the main thread) and on_done does NOT run."""
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool = registry.make_pool(AsyncPoolConfig())
        done_results: list = []
        errors: list[BaseException] = []

        boom = RuntimeError("boom")

        def bad():
            raise boom

        pool.submit(bad, on_done=done_results.append, on_error=errors.append)
        # Callback is queued but not yet fired (drain runs on this thread).
        _wait_for(lambda: not pool._pending.empty())
        assert errors == []
        pool.drain_main_thread_callbacks()
        assert done_results == []
        assert errors == [boom]
        assert isinstance(errors[0], RuntimeError)
    finally:
        registry.shutdown()


def test_on_error_runs_on_the_drain_caller_thread():
    """on_error, like on_done, fires on the thread that calls drain."""
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool = registry.make_pool(AsyncPoolConfig())
        observed_thread: list[int] = []

        def bad():
            raise RuntimeError("boom")

        pool.submit(
            bad,
            on_error=lambda _: observed_thread.append(threading.get_ident()),
        )
        _wait_for(lambda: not pool._pending.empty())
        my_tid = threading.get_ident()
        pool.drain_main_thread_callbacks()
        assert observed_thread == [my_tid]
    finally:
        registry.shutdown()


def test_on_done_exception_does_not_abort_remaining_callbacks():
    """One bad on_done shouldn't starve the others in the same drain."""
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool = registry.make_pool(AsyncPoolConfig())
        survivors: list = []

        def good_done(value):
            survivors.append(value)

        def bad_done(_):
            raise RuntimeError("on_done fail")

        pool.submit(lambda: "first", on_done=bad_done)
        pool.submit(lambda: "second", on_done=good_done)
        _wait_for(lambda: pool._pending.qsize() == 2)
        # Should NOT raise — exceptions are logged and skipped.
        pool.drain_main_thread_callbacks()
        assert survivors == ["second"]
    finally:
        registry.shutdown()


def test_submit_without_on_done_is_legal():
    """on_done is optional; fire-and-forget work just runs."""
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool = registry.make_pool(AsyncPoolConfig())
        ran = threading.Event()
        pool.submit(lambda: ran.set())
        assert ran.wait(timeout=1.0)
    finally:
        registry.shutdown()


def test_submit_after_close_raises():
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool = registry.make_pool(AsyncPoolConfig())
        pool.close()
        with pytest.raises(RuntimeError, match="closed"):
            pool.submit(lambda: 1)
    finally:
        registry.shutdown()


# ---- AsyncPool: dedicated mode -----------------------------------------

def test_dedicated_pool_uses_its_own_executor():
    """Dedicated pools own their executor and shut it down on close."""
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool = registry.make_pool(
            AsyncPoolConfig(mode="dedicated", max_workers=2)
        )
        assert pool._owns_executor is True
        assert pool._executor.__class__.__name__ == "ThreadPoolExecutor"

        # Sanity: it actually works.
        results: list = []
        pool.submit(lambda: 7, on_done=results.append)
        _wait_for(lambda: not pool._pending.empty())
        pool.drain_main_thread_callbacks()
        assert results == [7]
    finally:
        registry.shutdown()


def test_shared_pool_does_not_own_executor():
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool = registry.make_pool(AsyncPoolConfig())
        assert pool._owns_executor is False
    finally:
        registry.shutdown()


def test_invalid_mode_raises():
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        with pytest.raises(ValueError, match="shared"):
            registry.make_pool(AsyncPoolConfig(mode="bogus"))
    finally:
        registry.shutdown()


# ---- AsyncPoolRegistry: drain_all --------------------------------------

def test_drain_all_drains_every_pool_in_registration_order():
    registry = AsyncPoolRegistry(shared_workers=1)
    try:
        pool_a = registry.make_pool(AsyncPoolConfig())
        pool_b = registry.make_pool(AsyncPoolConfig())

        seen: list = []
        pool_a.submit(lambda: "a", on_done=seen.append)
        pool_b.submit(lambda: "b", on_done=seen.append)

        _wait_for(
            lambda: not pool_a._pending.empty() and not pool_b._pending.empty()
        )
        registry.drain_all()
        assert sorted(seen) == ["a", "b"]
    finally:
        registry.shutdown()


def test_remove_closes_pool_and_drops_from_registry():
    registry = AsyncPoolRegistry(shared_workers=1)
    pool = registry.make_pool(AsyncPoolConfig())
    registry.remove(pool)
    # After remove, drain_all must not invoke pool.
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)
    registry.shutdown()


def test_registry_shutdown_closes_all_pools():
    registry = AsyncPoolRegistry(shared_workers=1)
    pool = registry.make_pool(AsyncPoolConfig())
    registry.shutdown()
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)
