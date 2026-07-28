"""Bounded per-consumer delivery — how a slow observer stops throttling the device.

EVO-012. ``observe()`` is **pull**: the caller asks, one reading comes back, so
the rhythm is capture -> consume -> capture and the acquisition rate is bound by
the slowest thing downstream. Continuous acquisition breaks that coupling, and
the moment it does, the produced observations need somewhere to go that is
**bounded** — otherwise a consumer that cannot keep up trades a throttled device
for an exhausted heap.

Why per-consumer, not one shared ring
-------------------------------------
Because the drop policy is a **business** decision and different consumers give
opposite answers. Measured in pluck's burst path, the archival consumer must drop
the *newest* frame when full: the first frame of each lift is the reference the
motion verdict is computed against, so losing it voids the whole attempt. A
preview wants the exact opposite — it only ever wants the newest and the older
ones are worthless. One shared ring can only have one policy, so it cannot serve
both.

That is cheap to give them: the payload exists once and the queues hold
references (an :class:`~autoweaver.sensor.observation.Observation` is frozen with
a read-only payload, so sharing is safe). Memory is *frames alive* x *frame size*,
not queues x frame size.

No expiry, no stale reads
-------------------------
An offer either succeeds — and then that observation lives until its consumer is
done with it — or it **fails at offer time** and the caller is told immediately.
There is deliberately no ring wrap-around, no reference that goes bad underneath
a holder, and therefore no window in which a consumer can read something that has
already been recycled. Boundedness is enforced at the door, not by expiry.

(This supersedes EVO-011 §5.6's "expiry must have an explicit outcome": the
better answer turned out to be not having expiry at all.)

Dropping is accounted, never silent
-----------------------------------
Drops increment a counter and nothing else — no callback, no notification. The
owner collects the tally when it is convenient (typically at a task boundary,
on its own thread) via ``take_dropped``, which zeroes as it reads.

pluck's rule, kept verbatim because it is the whole point: **静默丢帧离线才发现,
那是最坏的情形** — silently dropped frames are only discovered offline, and that
is the worst case. A bounded queue that does not report is worse than an
unbounded one, because it fails invisibly.

Capacity is the caller's decision
---------------------------------
There is **no default capacity** anywhere in this module. One frame is 9.4 MB on
one of pluck's cameras and 35 MB on the other — a 4x spread on the same rig — so a
framework-chosen number would be wrong for someone by construction. The caller
knows its frame size, its service time and its memory budget; it passes a number.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from enum import Enum
from typing import Generic, Optional, TypeVar

from autoweaver.sensor.observation import Observation
from autoweaver.sensor.observer import Observer

logger = logging.getLogger(__name__)

#: What a queue carries. Observations are the reason this module exists and the
#: only thing ``QueuedDelivery`` accepts, but the queue itself never inspects its
#: items — the bounding and the drop policy are about *how many*, not *what*. It
#: is parameterised so other producers of expensive-to-handle work (the logbook's
#: attachment writer, whose items are file-write jobs) can reuse this policy
#: rather than growing a second, subtly different copy of it.
T = TypeVar("T")


class DropPolicy(Enum):
    """What a full queue does with the observation being offered.

    ``LATEST_ONLY``  — hold exactly one, always the newest; an offer never fails.
                       For consumers where old data has no value at all: a live
                       preview showing a stale frame is worse than showing none.

    ``DROP_NEWEST``  — refuse the incoming observation, keep what is queued.
                       For consumers whose **earliest** items are load-bearing:
                       pluck's archival path, where frame 1 of a burst is the
                       reference every later frame is compared against.

    ``DROP_OLDEST``  — evict the oldest queued item to make room.
                       For consumers that want the freshest N and treat a gap as
                       a glitch rather than a corruption.
    """

    LATEST_ONLY = "latest_only"
    DROP_NEWEST = "drop_newest"
    DROP_OLDEST = "drop_oldest"


class ObservationQueue(Generic[T]):
    """A bounded queue of observations with an explicit full-queue policy.

    ``offer`` **never blocks and never raises** — that is the contract that keeps
    the producer free. It returns whether the observation was accepted.

    Generic in what it carries (see :data:`T`): the bounding and the drop policy
    never look inside an item, so this is reusable by anything that has a fast
    producer, a slow consumer and a business opinion about which item to lose.
    Unparameterised, it means ``ObservationQueue[Observation]``.
    """

    def __init__(self, *, capacity: int, policy: DropPolicy) -> None:
        """``capacity`` is required and has no default — see the module docstring.

        ``LATEST_ONLY`` pins capacity to 1: "keep only the newest" and "keep a
        backlog" are contradictory instructions, and silently honouring the
        larger number would give a preview a queue of stale frames to work
        through.
        """
        if not isinstance(policy, DropPolicy):
            raise TypeError(f"policy must be a DropPolicy, got {policy!r}")
        capacity = int(capacity)
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._policy = policy
        self._capacity = 1 if policy is DropPolicy.LATEST_ONLY else capacity
        self._items: deque[T] = deque()
        self._cv = threading.Condition()
        self._dropped = 0
        self._closed = False

    # -- introspection ----------------------------------------------------- #

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def policy(self) -> DropPolicy:
        return self._policy

    @property
    def depth(self) -> int:
        """Observations waiting to be taken."""
        with self._cv:
            return len(self._items)

    # -- producer side ------------------------------------------------------ #

    def offer(self, observation: T) -> bool:
        """Enqueue one observation. Returns ``False`` if it was dropped.

        Never blocks, never raises. A ``False`` here is the *only* moment the
        producer learns about a drop, and it is why the answer is synchronous:
        the caller can account for it immediately instead of discovering the loss
        offline.
        """
        with self._cv:
            if self._closed:
                return False
            if len(self._items) < self._capacity:
                self._items.append(observation)
                self._cv.notify()
                return True

            if self._policy is DropPolicy.LATEST_ONLY:
                # Not counted as a drop: discarding the previous frame *is* the
                # requested behaviour here, not a failure to keep up.
                self._items.clear()
                self._items.append(observation)
                self._cv.notify()
                return True

            if self._policy is DropPolicy.DROP_OLDEST:
                self._items.popleft()
                self._items.append(observation)
                self._dropped += 1
                self._cv.notify()
                return True

            # DROP_NEWEST: the queued items are the valuable ones.
            self._dropped += 1
            return False

    # -- consumer side ------------------------------------------------------ #

    def take(self, timeout: Optional[float] = None) -> Optional[T]:
        """Pop the oldest observation, waiting up to ``timeout``.

        Returns ``None`` on timeout or once the queue is closed and drained.
        """
        with self._cv:
            deadline = None if timeout is None else time.monotonic() + timeout
            while not self._items:
                if self._closed:
                    return None
                if deadline is None:
                    self._cv.wait(0.2)
                else:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    self._cv.wait(min(0.05, left))
            return self._items.popleft()

    # -- accounting --------------------------------------------------------- #

    def take_dropped(self) -> int:
        """Return the drop tally **and zero it**.

        Pulled by the owner when convenient rather than pushed per drop: a drop
        storm (a stalled disk) would otherwise generate a callback per frame, on
        the producer's thread, at exactly the moment it is least affordable.
        """
        with self._cv:
            n, self._dropped = self._dropped, 0
            return n

    @property
    def dropped(self) -> int:
        """Current tally without clearing it — for assertions and diagnostics."""
        with self._cv:
            return self._dropped

    # -- shutdown ----------------------------------------------------------- #

    def close(self) -> None:
        """Refuse further offers and wake anyone blocked in :meth:`take`."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    @property
    def closed(self) -> bool:
        with self._cv:
            return self._closed


class QueuedDelivery:
    """A bounded queue plus one thread that feeds it to a single Observer.

    This is the shape that lets a slow consumer exist at all without it becoming
    the system's clock: the producer's cost per observation is one ``offer``, and
    everything expensive happens on this thread.

    One thread, FIFO — so delivery order equals acquisition order. Consumers that
    number their outputs, or compare each item against the first of a run, depend
    on that. Raising throughput by adding threads would break the ordering those
    consumers rely on; the fix for a genuinely overloaded consumer is more
    *consumers* with their own queues, or a faster one — not more threads on this
    queue.
    """

    def __init__(
        self,
        observer: Observer,
        *,
        capacity: int,
        policy: DropPolicy,
        name: str = "",
    ) -> None:
        self._observer = observer
        self._queue = ObservationQueue(capacity=capacity, policy=policy)
        self._name = name or f"delivery-{getattr(observer, 'name', 'observer')}"
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()

    # -- introspection ----------------------------------------------------- #

    @property
    def observer(self) -> Observer:
        return self._observer

    @property
    def queue(self) -> ObservationQueue:
        return self._queue

    @property
    def name(self) -> str:
        return self._name

    # -- producer side ------------------------------------------------------ #

    def offer(self, observation: Observation) -> bool:
        """Hand one observation to this consumer. Never blocks, never raises."""
        accepted = self._queue.offer(observation)
        if accepted:
            self._ensure_thread()
        return accepted

    def take_dropped(self) -> int:
        """Drop tally for this consumer, zeroed as it is read."""
        return self._queue.take_dropped()

    # -- delivery thread ---------------------------------------------------- #

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name=self._name
            )
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            observation = self._queue.take(timeout=0.2)
            if observation is None:
                if self._queue.closed:
                    return
                continue
            self._idle.clear()
            try:
                self._observer.on_observation(observation)
            except Exception:  # noqa: BLE001 - one bad consumer must not end delivery
                logger.exception(
                    "delivery '%s': observer raised on observation %d",
                    self._name, observation.id,
                )
            finally:
                if self._queue.depth == 0:
                    self._idle.set()

    # -- shutdown ----------------------------------------------------------- #

    def drain(self, timeout: float) -> bool:
        """Wait for the backlog to be delivered. ``False`` means it timed out.

        **Call this before tearing down**, or whatever is still queued vanishes
        with the process — the loss would be real and unrecorded, which is the
        one outcome this module exists to prevent.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.depth == 0 and self._idle.is_set():
                return True
            if self._thread is None or not self._thread.is_alive():
                return self._queue.depth == 0
            self._idle.wait(0.02)
        return self._queue.depth == 0 and self._idle.is_set()

    def close(self, timeout: float = 2.0) -> bool:
        """Drain, then stop the thread. Returns the drain result.

        Anything still queued after ``timeout`` is abandoned rather than waited
        on: this runs during teardown, where blocking forever turns Ctrl+C into a
        hang. Whatever was abandoned is still on the drop tally.
        """
        drained = self.drain(timeout)
        remaining = self._queue.depth
        self._stop.set()
        self._queue.close()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)
        if remaining:
            logger.warning(
                "delivery '%s': abandoned %d undelivered observation(s) at close",
                self._name, remaining,
            )
        return drained


__all__ = ["DropPolicy", "ObservationQueue", "QueuedDelivery"]
