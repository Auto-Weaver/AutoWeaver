"""Tests for bounded per-consumer delivery (EVO-012).

The queue exists so a slow consumer stops throttling the device. What must not
be left to convention: the full-queue policy actually differs per consumer,
offering never blocks, and every drop is counted and collectable.

No test here sleeps to make an assertion true — threads are gated with Events so
a slow machine cannot turn a real failure into a pass.
"""

from __future__ import annotations

import threading

import pytest

from autoweaver.sensor.delivery import DropPolicy, ObservationQueue, QueuedDelivery
from autoweaver.sensor.observation import Observation
from autoweaver.sensor.observer import Observer, ObserverSpeed


def make_observation(obs_id: int, source: str = "nest") -> Observation:
    return Observation(id=obs_id, source=source, captured_at=float(obs_id), data=obs_id)


class CollectingObserver(Observer):
    """Records what it is handed. ``gate`` (if set) blocks each delivery until
    released, which is how "the consumer is slow" is expressed deterministically."""

    def __init__(self, speed: ObserverSpeed = ObserverSpeed.SLOW, gate=None, boom=False):
        self._speed = speed
        self._gate = gate
        self._boom = boom
        self.seen: list = []
        self.entered = threading.Semaphore(0)

    @property
    def speed(self) -> ObserverSpeed:
        return self._speed

    def on_observation(self, observation: Observation) -> None:
        self.entered.release()
        if self._gate is not None:
            self._gate.wait()
        if self._boom:
            raise RuntimeError("observer exploded")
        self.seen.append(observation.id)


# --------------------------------------------------------------------------- #
# ObservationQueue — capacity and policy
# --------------------------------------------------------------------------- #


def test_capacity_is_required_no_framework_default():
    """A capacity guessed by the framework is wrong by construction (9.4 MB vs
    35 MB per frame on the same rig), so there is no default to fall back on."""
    with pytest.raises(TypeError):
        ObservationQueue(policy=DropPolicy.DROP_NEWEST)  # type: ignore[call-arg]


def test_policy_must_be_a_drop_policy():
    with pytest.raises(TypeError):
        ObservationQueue(capacity=4, policy="drop_newest")  # type: ignore[arg-type]


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        ObservationQueue(capacity=0, policy=DropPolicy.DROP_NEWEST)


def test_latest_only_pins_capacity_to_one():
    """"Keep only the newest" and "keep a backlog" are contradictory; honouring
    the larger number would hand a preview a queue of stale frames."""
    q = ObservationQueue(capacity=10, policy=DropPolicy.LATEST_ONLY)
    assert q.capacity == 1


def test_drop_newest_keeps_the_earliest_and_rejects_the_incoming():
    """pluck's archival case: frame 1 of a burst is the reference every later
    frame is compared against, so losing it voids the whole attempt."""
    q = ObservationQueue(capacity=2, policy=DropPolicy.DROP_NEWEST)
    assert q.offer(make_observation(1)) is True
    assert q.offer(make_observation(2)) is True
    assert q.offer(make_observation(3)) is False        # rejected at the door
    assert q.depth == 2
    assert q.take(timeout=0).id == 1                    # the reference survived
    assert q.take(timeout=0).id == 2
    assert q.dropped == 1


def test_drop_oldest_evicts_to_admit_the_newest():
    q = ObservationQueue(capacity=2, policy=DropPolicy.DROP_OLDEST)
    for i in (1, 2, 3):
        assert q.offer(make_observation(i)) is True
    assert [q.take(timeout=0).id for _ in range(2)] == [2, 3]
    assert q.dropped == 1


def test_latest_only_always_accepts_and_holds_just_the_newest():
    q = ObservationQueue(capacity=1, policy=DropPolicy.LATEST_ONLY)
    for i in (1, 2, 3):
        assert q.offer(make_observation(i)) is True
    assert q.depth == 1
    assert q.take(timeout=0).id == 3


def test_latest_only_overwrites_are_not_counted_as_drops():
    """Discarding the previous frame *is* the requested behaviour for a preview,
    not a failure to keep up — counting it would cry wolf on every frame."""
    q = ObservationQueue(capacity=1, policy=DropPolicy.LATEST_ONLY)
    for i in range(5):
        q.offer(make_observation(i))
    assert q.dropped == 0


def test_take_dropped_zeroes_the_tally():
    q = ObservationQueue(capacity=1, policy=DropPolicy.DROP_NEWEST)
    q.offer(make_observation(1))
    q.offer(make_observation(2))
    q.offer(make_observation(3))
    assert q.take_dropped() == 2
    assert q.take_dropped() == 0


def test_offer_never_blocks_when_full():
    """The producer's cost is one non-blocking offer — that is the property the
    whole design rests on."""
    q = ObservationQueue(capacity=1, policy=DropPolicy.DROP_NEWEST)
    q.offer(make_observation(1))
    done = threading.Event()

    def produce():
        for i in range(200):
            q.offer(make_observation(i))
        done.set()

    threading.Thread(target=produce, daemon=True).start()
    assert done.wait(5.0), "offer() blocked on a full queue"


def test_closed_queue_refuses_offers():
    q = ObservationQueue(capacity=2, policy=DropPolicy.DROP_NEWEST)
    q.close()
    assert q.offer(make_observation(1)) is False
    assert q.closed is True


def test_take_returns_none_on_closed_and_drained_queue():
    q = ObservationQueue(capacity=2, policy=DropPolicy.DROP_NEWEST)
    q.close()
    assert q.take(timeout=0.01) is None


# --------------------------------------------------------------------------- #
# QueuedDelivery — the queue plus its own thread
# --------------------------------------------------------------------------- #


def test_delivery_hands_observations_over_in_order():
    observer = CollectingObserver()
    delivery = QueuedDelivery(observer, capacity=8, policy=DropPolicy.DROP_NEWEST)
    try:
        for i in range(5):
            assert delivery.offer(make_observation(i)) is True
        assert delivery.drain(timeout=5.0)
        assert observer.seen == [0, 1, 2, 3, 4]
    finally:
        delivery.close()


def test_slow_consumer_does_not_block_the_producer():
    """The point of the cut: with the consumer wedged, the producer still gets
    through every offer, and the overflow lands on the tally instead of on the
    device."""
    gate = threading.Event()
    observer = CollectingObserver(gate=gate)
    delivery = QueuedDelivery(observer, capacity=2, policy=DropPolicy.DROP_NEWEST)
    try:
        assert delivery.offer(make_observation(0)) is True
        assert observer.entered.acquire(timeout=5.0), "delivery thread never started"
        # Consumer is now wedged inside on_observation. Fill the queue and beyond.
        accepted = [delivery.offer(make_observation(i)) for i in range(1, 20)]
        assert accepted.count(True) <= 2          # bounded, as configured
        assert accepted.count(False) >= 1         # the rest refused at the door
        assert delivery.queue.dropped == accepted.count(False)
    finally:
        gate.set()
        delivery.close()


def test_delivery_survives_an_observer_that_raises():
    """A crashing consumer must not end delivery for the ones behind it."""
    boom = CollectingObserver(boom=True)
    delivery = QueuedDelivery(boom, capacity=4, policy=DropPolicy.DROP_NEWEST)
    try:
        delivery.offer(make_observation(1))
        delivery.offer(make_observation(2))
        assert delivery.drain(timeout=5.0)
        assert boom.entered.acquire(timeout=5.0)
        assert boom.entered.acquire(timeout=5.0)   # it was called twice
    finally:
        delivery.close()


def test_close_drains_what_was_already_accepted():
    """Anything accepted must still reach its consumer — an accepted observation
    that vanishes at teardown is exactly the silent loss this module prevents."""
    observer = CollectingObserver()
    delivery = QueuedDelivery(observer, capacity=16, policy=DropPolicy.DROP_NEWEST)
    for i in range(10):
        delivery.offer(make_observation(i))
    assert delivery.close(timeout=5.0) is True
    assert observer.seen == list(range(10))


def test_close_stops_the_delivery_thread():
    observer = CollectingObserver()
    delivery = QueuedDelivery(observer, capacity=4, policy=DropPolicy.DROP_NEWEST)
    delivery.offer(make_observation(1))
    delivery.close(timeout=5.0)
    names = [t.name for t in threading.enumerate() if t.name == delivery.name]
    assert names == [], f"delivery thread leaked: {names}"
