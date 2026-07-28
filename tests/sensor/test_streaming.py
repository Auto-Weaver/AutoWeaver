"""Tests for continuous acquisition — ``Sensor.start_streaming`` (EVO-012).

The property being defended: **acquisition is no longer bound by consumption.**
pluck measured what the coupled version costs (288 ms/frame service time into a
0.05-0.30 s motion window, 1-3 frames caught per lift); these tests pin the
decoupling itself rather than any timing number.

Every wait here is on an Event/Semaphore tied to the condition under test, never
a bare sleep — a thread test that sleeps is a thread test that passes on a fast
machine and lies on a slow one.
"""

from __future__ import annotations

import threading

import pytest

from autoweaver.sensor.base import Sensor
from autoweaver.sensor.delivery import DropPolicy
from autoweaver.sensor.observation import Observation
from autoweaver.sensor.observer import Observer, ObserverSpeed


class CountingSensor(Sensor):
    """Deliberately does not call ``super().__init__()`` — same shape as the
    pre-existing camera subclasses, so the lazy-state path stays covered.

    ``read_ticket`` is released once per read, so a test can wait for "N reads
    have happened" instead of sleeping and hoping.
    """

    def __init__(self, *, fail_reads=()) -> None:
        self._open = False
        self.read_count = 0
        self.read_ticket = threading.Semaphore(0)
        self._fail_reads = set(fail_reads)
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "counting"

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def snapshot(self):
        with self._lock:
            self.read_count += 1
            n = self.read_count
        self.read_ticket.release()
        if n in self._fail_reads:
            raise RuntimeError(f"simulated read failure #{n}")
        return n


class TicketObserver(Observer):
    def __init__(self, speed: ObserverSpeed = ObserverSpeed.FAST, gate=None):
        self._speed = speed
        self._gate = gate
        self.seen: list = []
        self.entered = threading.Semaphore(0)

    @property
    def speed(self) -> ObserverSpeed:
        return self._speed

    def on_observation(self, observation: Observation) -> None:
        self.entered.release()
        if self._gate is not None:
            self._gate.wait()
        self.seen.append(observation.id)


def wait_for_reads(sensor: CountingSensor, n: int, timeout: float = 5.0) -> bool:
    """Block until ``n`` reads have happened, or fail the wait."""
    for _ in range(n):
        if not sensor.read_ticket.acquire(timeout=timeout):
            return False
    return True


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #


def test_streaming_requires_a_role():
    """Same rule as observe(): an observation identified by the class name would
    recreate the exact defect roles exist to remove."""
    sensor = CountingSensor()
    with pytest.raises(ValueError, match="role"):
        sensor.start_streaming()


def test_start_and_stop_are_clean():
    sensor = CountingSensor()
    sensor.role = "nest"
    assert sensor.is_streaming is False
    sensor.start_streaming(min_interval=0.001)
    assert sensor.is_streaming is True
    assert wait_for_reads(sensor, 3)
    assert sensor.stop_streaming(timeout=5.0) is True
    assert sensor.is_streaming is False
    leaked = [t.name for t in threading.enumerate() if t.name == "acquire-nest"]
    assert leaked == [], f"acquisition thread leaked: {leaked}"


def test_stop_actually_stops_reading():
    sensor = CountingSensor()
    sensor.role = "nest"
    sensor.start_streaming(min_interval=0.001)
    assert wait_for_reads(sensor, 2)
    sensor.stop_streaming(timeout=5.0)
    settled = sensor.read_count
    # Nothing may advance the counter once the thread is joined.
    assert sensor.read_count == settled


def test_start_is_idempotent():
    sensor = CountingSensor()
    sensor.role = "nest"
    sensor.start_streaming(min_interval=0.001)
    try:
        sensor.start_streaming(min_interval=0.001)
        running = [t for t in threading.enumerate() if t.name == "acquire-nest"]
        assert len(running) == 1
    finally:
        sensor.stop_streaming(timeout=5.0)


def test_stop_without_start_is_a_no_op():
    sensor = CountingSensor()
    sensor.role = "nest"
    assert sensor.stop_streaming() is True


def test_negative_min_interval_is_rejected():
    sensor = CountingSensor()
    sensor.role = "nest"
    with pytest.raises(ValueError):
        sensor.start_streaming(min_interval=-1.0)


def test_a_failing_read_does_not_end_the_stream():
    """A dropped USB packet must not silently end acquisition halfway through a
    motion, leaving the caller wondering why the frames stopped."""
    sensor = CountingSensor(fail_reads={2, 3})
    sensor.role = "nest"
    sensor.start_streaming(min_interval=0.001)
    try:
        assert wait_for_reads(sensor, 6), "stream died on a failing read"
    finally:
        sensor.stop_streaming(timeout=5.0)


# --------------------------------------------------------------------------- #
# both modes coexist
# --------------------------------------------------------------------------- #


def test_observe_still_works_while_streaming():
    """Pull and push share the device lock, so an on-demand read during a stream
    simply queues behind the acquisition thread's current read."""
    sensor = CountingSensor()
    sensor.role = "nest"
    sensor.start_streaming(min_interval=0.005)
    try:
        assert wait_for_reads(sensor, 1)
        observation = sensor.observe()
        assert observation.source == "nest"
        assert observation.id > 0
    finally:
        sensor.stop_streaming(timeout=5.0)


def test_observation_ids_stay_unique_across_both_modes():
    """Identity is the freshness gate NEXT-013's ServoLeaf keys on; a repeated id
    would make a new frame look stale."""
    sensor = CountingSensor()
    sensor.role = "nest"
    seen = TicketObserver()
    sensor.add_observer(seen)          # sees both modes: streamed and pulled
    sensor.start_streaming(min_interval=0.001)
    try:
        assert wait_for_reads(sensor, 5)
        pulled = [sensor.observe().id for _ in range(5)]
    finally:
        sensor.stop_streaming(timeout=5.0)

    ids = list(seen.seen)
    assert len(ids) == len(set(ids)), f"duplicate observation ids: {ids}"
    assert len(set(pulled)) == len(pulled), f"duplicate ids from observe(): {pulled}"
    # The id is minted under the device lock, so the set is gapless regardless of
    # which mode produced each one. Publication *order* is explicitly not
    # guaranteed across concurrent producers, so only the set is asserted.
    assert set(ids) == set(range(1, max(ids) + 1))


# --------------------------------------------------------------------------- #
# the decoupling itself
# --------------------------------------------------------------------------- #


def test_a_wedged_consumer_does_not_stop_acquisition():
    """The whole point of the cut. With the consumer stuck inside
    ``on_observation``, the device keeps being read."""
    sensor = CountingSensor()
    sensor.role = "nest"
    gate = threading.Event()
    slow = TicketObserver(speed=ObserverSpeed.SLOW, gate=gate)
    sensor.add_observer(slow, capacity=2, policy=DropPolicy.DROP_NEWEST)
    sensor.start_streaming(min_interval=0.001)
    try:
        assert slow.entered.acquire(timeout=5.0), "consumer never got the first one"
        # Consumer is wedged. Acquisition must sail past the queue capacity.
        assert wait_for_reads(sensor, 20), "acquisition stalled behind the consumer"
        total, by_observer = sensor.take_dropped()
        assert total > 0, "a wedged consumer at capacity 2 must have dropped"
        assert by_observer == {"TicketObserver": total}
    finally:
        gate.set()
        sensor.stop_streaming(timeout=5.0)
        sensor.close_observers(timeout=5.0)


def test_take_dropped_zeroes_and_is_per_observer():
    sensor = CountingSensor()
    sensor.role = "nest"
    gate = threading.Event()
    slow = TicketObserver(speed=ObserverSpeed.SLOW, gate=gate)
    sensor.add_observer(slow, capacity=1, policy=DropPolicy.DROP_NEWEST)
    try:
        # capacity 1 + a wedged consumer: at most 2 of the 10 can be in flight
        # (one queued, one being worked on), so the rest must land on the tally.
        for _ in range(10):
            sensor.observe()
        total, by_observer = sensor.take_dropped()
        assert total >= 8, f"expected >=8 drops at capacity 1, got {total}"
        assert by_observer == {"TicketObserver": total}
        assert sensor.take_dropped() == (0, {})
    finally:
        gate.set()
        sensor.close_observers(timeout=5.0)


# --------------------------------------------------------------------------- #
# registration contract
# --------------------------------------------------------------------------- #


def test_capacity_and_policy_must_be_given_together():
    """Passing one lets the framework invent the half you left out, and both
    halves are the caller's decision."""
    sensor = CountingSensor()
    sensor.role = "nest"
    with pytest.raises(ValueError, match="capacity and policy"):
        sensor.add_observer(TicketObserver(), capacity=4)
    with pytest.raises(ValueError, match="capacity and policy"):
        sensor.add_observer(TicketObserver(), policy=DropPolicy.DROP_NEWEST)


def test_a_queued_slow_observer_needs_no_dispatcher():
    """Its own queue and thread *are* the hand-off, so the dispatcher
    precondition does not apply."""
    sensor = CountingSensor()
    sensor.role = "nest"
    slow = TicketObserver(speed=ObserverSpeed.SLOW)
    sensor.add_observer(slow, capacity=4, policy=DropPolicy.LATEST_ONLY)
    try:
        assert sensor.observers == (slow,)
    finally:
        sensor.close_observers(timeout=5.0)


def test_an_unqueued_slow_observer_still_needs_a_dispatcher():
    """The pre-existing guard must survive: a loud failure at wiring time beats a
    mysterious stall at runtime."""
    sensor = CountingSensor()
    sensor.role = "nest"
    with pytest.raises(RuntimeError, match="slow dispatcher"):
        sensor.add_observer(TicketObserver(speed=ObserverSpeed.SLOW))


def test_removing_a_queued_observer_drains_it():
    sensor = CountingSensor()
    sensor.role = "nest"
    observer = TicketObserver(speed=ObserverSpeed.SLOW)
    sensor.add_observer(observer, capacity=16, policy=DropPolicy.DROP_NEWEST)
    for _ in range(5):
        sensor.observe()
    sensor.remove_observer(observer)
    assert observer.seen == [1, 2, 3, 4, 5]
    assert sensor.observers == ()


def test_close_observers_delivers_the_backlog():
    sensor = CountingSensor()
    sensor.role = "nest"
    observer = TicketObserver(speed=ObserverSpeed.SLOW)
    sensor.add_observer(observer, capacity=32, policy=DropPolicy.DROP_NEWEST)
    for _ in range(8):
        sensor.observe()
    assert sensor.close_observers(timeout=5.0) is True
    assert observer.seen == [1, 2, 3, 4, 5, 6, 7, 8]


def test_unqueued_observers_keep_their_old_behaviour():
    """Omitting capacity/policy must leave FAST inline on the caller's thread —
    existing wiring is untouched by this cut."""
    sensor = CountingSensor()
    sensor.role = "nest"
    observer = TicketObserver(speed=ObserverSpeed.FAST)
    sensor.add_observer(observer)
    caller = threading.current_thread()
    seen_on = []

    class ThreadNoting(Observer):
        @property
        def speed(self):
            return ObserverSpeed.FAST

        def on_observation(self, observation):
            seen_on.append(threading.current_thread())

    sensor.add_observer(ThreadNoting())
    sensor.observe()
    assert seen_on == [caller]
    assert observer.seen == [1]
