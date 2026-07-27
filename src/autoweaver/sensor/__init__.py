"""Sensor abstractions — device drivers held by Workers, and what they produce.

EVO-011: a Sensor no longer hands out a bare value. ``observe()`` mints an
:class:`~autoweaver.sensor.observation.Observation` — a reading that knows who
took it, when, and under what conditions — and pushes it to registered
:class:`~autoweaver.sensor.observer.Observer` s.
"""

from autoweaver.sensor.base import Sensor, SlowDispatcher
from autoweaver.sensor.observation import Derivation, Observation, PixelTransform
from autoweaver.sensor.observer import Observer, ObserverSpeed

__all__ = [
    "Derivation",
    "Observation",
    "Observer",
    "ObserverSpeed",
    "PixelTransform",
    "Sensor",
    "SlowDispatcher",
]
