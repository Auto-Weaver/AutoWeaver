"""Sensor abstractions — device drivers held by Workers, and what they produce.

EVO-011: a Sensor no longer hands out a bare value. ``observe()`` mints an
:class:`~autoweaver.sensor.observation.Observation` — a reading that knows who
took it, when, and under what conditions — and pushes it to registered
:class:`~autoweaver.sensor.observer.Observer` s.

EVO-012: ``observe()`` is the *on demand* mode. ``Sensor.start_streaming()`` adds
the *continuous* one — an acquisition thread that reads at the device's own
rhythm — and :mod:`autoweaver.sensor.delivery` gives each consumer a bounded
queue with its own drop policy, so a slow one drops observations instead of
throttling the device.
"""

from autoweaver.sensor.base import Sensor, SlowDispatcher
from autoweaver.sensor.delivery import DropPolicy, ObservationQueue, QueuedDelivery
from autoweaver.sensor.observation import Derivation, Observation, PixelTransform
from autoweaver.sensor.observer import Observer, ObserverSpeed

__all__ = [
    "Derivation",
    "DropPolicy",
    "Observation",
    "ObservationQueue",
    "Observer",
    "ObserverSpeed",
    "PixelTransform",
    "QueuedDelivery",
    "Sensor",
    "SlowDispatcher",
]
