"""Tests for Sensor.observe() — minting, serialisation and observer fan-out.

EVO-011: drive chain BT -> Sensor -> Observer. These cover the parts the design
says must not be left to convention: identity per role, one-driver-at-a-time
without a thread, and the fast/slow declaration.
"""

from __future__ import annotations

import threading
import time
from typing import List

import numpy as np
import pytest

from autoweaver.camera.base import CameraConfig
from autoweaver.camera.mock import MockCamera
from autoweaver.camera.observation import CameraObservation
from autoweaver.sensor.base import Sensor
from autoweaver.sensor.observation import Observation
from autoweaver.sensor.observer import Observer, ObserverSpeed


class FakeSensor(Sensor):
    """Minimal Sensor that does not call ``super().__init__()`` — exactly like the
    pre-existing camera subclasses, so the lazy-state path stays covered."""

    def __init__(self, readings=None, read_delay: float = 0.0) -> None:
        self._readings = readings
        self._read_delay = read_delay
        self._open = False
        self.read_count = 0

    @property
    def name(self) -> str:
        return "fake"

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def snapshot(self):
        if self._read_delay:
            time.sleep(self._read_delay)
        self.read_count += 1
        if self._readings is None:
            return self.read_count
        return self._readings[(self.read_count - 1) % len(self._readings)]


class RecordingObserver(Observer):
    def __init__(self, speed: ObserverSpeed = ObserverSpeed.FAST, boom: bool = False):
        self._speed = speed
        self._boom = boom
        self.seen: List[Observation] = []
        self.threads: List[int] = []

    @property
    def speed(self) -> ObserverSpeed:
        return self._speed

    def on_observation(self, observation: Observation) -> None:
        self.seen.append(observation)
        self.threads.append(threading.get_ident())
        if self._boom:
            raise RuntimeError("observer exploded")


def _camera(**config) -> MockCamera:
    cam = MockCamera(CameraConfig(**config), mode="random", width=32, height=24)
    cam.open()
    cam.role = "nest"
    return cam


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

def test_observe_requires_a_role():
    """Silently defaulting to the class name is the exact defect this removes:
    two Daheng cameras would both be 'DahengCamera'."""
    sensor = FakeSensor()
    with pytest.raises(ValueError, match="role"):
        sensor.observe()


def test_role_rejects_empty_values():
    sensor = FakeSensor()
    with pytest.raises(ValueError):
        sensor.role = "   "


def test_observation_ids_are_monotonic():
    sensor = FakeSensor()
    sensor.role = "nest"
    assert [sensor.observe().id for _ in range(3)] == [1, 2, 3]


def test_observation_ids_are_isolated_per_source():
    """Each sensor counts on its own, so ids never interleave across roles."""
    nest, drill = FakeSensor(), FakeSensor()
    nest.role, drill.role = "nest", "drill"

    nest.observe()
    nest.observe()
    drill_first = drill.observe()

    assert drill_first.id == 1
    assert drill_first.source == "drill"
    assert nest.observe().id == 3


def test_observation_carries_source_and_capture_time():
    sensor = FakeSensor()
    sensor.role = "nest"
    before = time.monotonic()
    observation = sensor.observe()
    assert observation.source == "nest"
    assert before <= observation.captured_at <= time.monotonic()


def test_projection_is_passed_by_reference():
    """The model travels with the data instead of being looked up separately."""
    model = object()
    sensor = FakeSensor()
    sensor.role = "nest"
    sensor.projection = model
    assert sensor.observe().projection is model


def test_camera_observe_returns_camera_observation_with_conditions():
    """Imaging conditions only the device knows must ride along — today they
    survive only as a YAML comment tying exposure to a detection threshold."""
    cam = _camera(exposure_time=12000, gain=1.5)
    try:
        observation = cam.observe()
        assert isinstance(observation, CameraObservation)
        assert observation.conditions["exposure_time"] == 12000
        assert observation.conditions["gain"] == 1.5
        assert observation.data.shape == (24, 32, 3)
    finally:
        cam.close()


def test_camera_without_config_reports_no_conditions():
    """Report what the device knows; do not invent defaults."""
    sensor = FakeSensor()
    sensor.role = "nest"
    assert dict(sensor.observe().conditions) == {}


# --------------------------------------------------------------------------- #
# serialisation
# --------------------------------------------------------------------------- #

def test_observe_is_serialised_across_threads():
    """A lock, not a thread. This is what removes the hand-written ThreadSafe*
    wrapper plus ad-hoc 'yield to the burst' arbitration."""
    sensor = FakeSensor(read_delay=0.01)
    sensor.role = "nest"
    overlap = {"max": 0, "current": 0}
    guard = threading.Lock()
    original_snapshot = sensor.snapshot

    def instrumented():
        with guard:
            overlap["current"] += 1
            overlap["max"] = max(overlap["max"], overlap["current"])
        try:
            return original_snapshot()
        finally:
            with guard:
                overlap["current"] -= 1

    sensor.snapshot = instrumented  # type: ignore[method-assign]

    threads = [threading.Thread(target=sensor.observe) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert overlap["max"] == 1, "device reads overlapped despite the observe lock"
    assert sensor.read_count == 6


def test_concurrent_observe_ids_are_unique():
    sensor = FakeSensor(read_delay=0.002)
    sensor.role = "nest"
    seen: List[int] = []
    seen_lock = threading.Lock()

    def take():
        observation = sensor.observe()
        with seen_lock:
            seen.append(observation.id)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(seen) == list(range(1, 9))


# --------------------------------------------------------------------------- #
# observer fan-out
# --------------------------------------------------------------------------- #

def test_fast_observers_receive_observations_in_order():
    sensor = FakeSensor()
    sensor.role = "nest"
    first, second = RecordingObserver(), RecordingObserver()
    sensor.add_observer(first)
    sensor.add_observer(second)

    sensor.observe()
    sensor.observe()

    assert [o.id for o in first.seen] == [1, 2]
    assert [o.id for o in second.seen] == [1, 2]
    assert sensor.observers == (first, second)


def test_fast_observer_runs_on_the_calling_thread():
    sensor = FakeSensor()
    sensor.role = "nest"
    observer = RecordingObserver(ObserverSpeed.FAST)
    sensor.add_observer(observer)

    sensor.observe()

    assert observer.threads == [threading.get_ident()]


def test_slow_observer_requires_a_dispatcher():
    """A loud failure at wiring time beats a mysterious tick stall at runtime."""
    sensor = FakeSensor()
    sensor.role = "nest"
    with pytest.raises(RuntimeError, match="dispatcher"):
        sensor.add_observer(RecordingObserver(ObserverSpeed.SLOW))


def test_slow_observer_goes_through_the_dispatcher():
    sensor = FakeSensor()
    sensor.role = "nest"
    deferred = []
    sensor.set_slow_dispatcher(deferred.append)
    observer = RecordingObserver(ObserverSpeed.SLOW)
    sensor.add_observer(observer)

    sensor.observe()

    assert observer.seen == [], "slow observer must not run inline"
    assert len(deferred) == 1
    deferred[0]()
    assert [o.id for o in observer.seen] == [1]


def test_speed_can_be_overridden_at_registration():
    sensor = FakeSensor()
    sensor.role = "nest"
    deferred = []
    sensor.set_slow_dispatcher(deferred.append)
    observer = RecordingObserver(ObserverSpeed.FAST)

    sensor.add_observer(observer, speed=ObserverSpeed.SLOW)
    sensor.observe()

    assert observer.seen == []
    assert len(deferred) == 1


def test_registration_rejects_a_missing_speed_declaration():
    class SpeedlessObserver(RecordingObserver):
        @property
        def speed(self):
            return "fast"  # not an ObserverSpeed

    sensor = FakeSensor()
    sensor.role = "nest"
    with pytest.raises(TypeError, match="ObserverSpeed"):
        sensor.add_observer(SpeedlessObserver())


def test_observer_exception_is_isolated():
    """A crashing preview must not take perception — or its siblings — down."""
    sensor = FakeSensor()
    sensor.role = "nest"
    exploding = RecordingObserver(boom=True)
    healthy = RecordingObserver()
    sensor.add_observer(exploding)
    sensor.add_observer(healthy)

    observation = sensor.observe()

    assert observation.id == 1
    assert [o.id for o in healthy.seen] == [1]


def test_observer_can_be_removed_and_is_not_added_twice():
    sensor = FakeSensor()
    sensor.role = "nest"
    observer = RecordingObserver()

    sensor.add_observer(observer)
    sensor.add_observer(observer)
    assert sensor.observers == (observer,)

    sensor.remove_observer(observer)
    sensor.remove_observer(observer)  # no-op, must not raise
    sensor.observe()
    assert observer.seen == []


def test_sensor_never_spawns_a_thread():
    """'no internal heartbeat, no thread' must survive this cut."""
    sensor = FakeSensor()
    sensor.role = "nest"
    sensor.add_observer(RecordingObserver())
    before = threading.active_count()

    for _ in range(5):
        sensor.observe()

    assert threading.active_count() == before


# --------------------------------------------------------------------------- #
# backward compatibility
# --------------------------------------------------------------------------- #

def test_legacy_snapshot_and_capture_still_work():
    """pluck still drives cameras through capture()/is_opened(); this cut adds
    observe() alongside them rather than replacing them."""
    cam = _camera()
    try:
        assert isinstance(cam.snapshot(), np.ndarray)
        assert isinstance(cam.capture(), np.ndarray)
        assert cam.is_opened() is True
    finally:
        cam.close()


def test_legacy_snapshot_does_not_require_a_role_or_notify_observers():
    """The old path is untouched: no role needed, no fan-out, no read-only flag."""
    sensor = FakeSensor()
    observer = RecordingObserver()
    sensor.role = "nest"
    sensor.add_observer(observer)

    reading = sensor.snapshot()

    assert reading == 1
    assert observer.seen == []


def test_snapshot_is_still_the_acquisition_hook():
    """observe() reads through snapshot(), so a subclass customising acquisition
    there is honoured. (Overriding only capture() is NOT — see CameraBase docs.)"""
    class CustomSensor(FakeSensor):
        def snapshot(self):
            return "custom"

    sensor = CustomSensor()
    sensor.role = "nest"
    assert sensor.observe().data == "custom"
