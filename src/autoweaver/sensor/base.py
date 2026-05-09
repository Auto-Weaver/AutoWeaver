"""Sensor base class — passive device driver.

A Sensor is a stateful device driver held by a Subsystem. It does NOT
respond to ticks; its node just exposes open / close / snapshot /
configure for the subsystem to call. See EVO-006.

Continuous sensors (pressure, distance) and triggered sensors (cameras)
share the same shape — the Subsystem decides per-tick whether to call
``snapshot``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Sensor(ABC):
    """Abstract device driver.

    Subsystems hold one or more Sensors. The Sensor itself is passive:
    no internal heartbeat, no thread (other than what the device SDK
    requires), no tick handling. Its only job is to expose:

      - ``open / close``        — lifecycle, called by Subsystem on_start / on_stop
      - ``is_open``              — query
      - ``snapshot``             — return current reading (synchronous)
      - ``configure``            — set device parameters

    ``snapshot`` may be slow (e.g. waiting for a fresh frame from a
    camera). Subsystems are responsible for calling it from
    ``run_async`` if it doesn't fit a single tick budget.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier for logs and metrics."""

    @abstractmethod
    def open(self) -> None:
        """Acquire device resources. Idempotent if already open."""

    @abstractmethod
    def close(self) -> None:
        """Release device resources. Idempotent if already closed."""

    @abstractmethod
    def is_open(self) -> bool:
        """Whether the device is currently open and ready."""

    @abstractmethod
    def snapshot(self) -> Any:
        """Return the current reading.

        For triggered devices (camera): captures and returns a fresh
        sample.
        For continuous devices (pressure, distance): returns the latest
        observed value.

        Implementations should raise rather than return stale / sentinel
        values when the device is unavailable.
        """

    def configure(self, **kwargs: Any) -> None:
        """Apply device parameters. Default no-op; subclasses override
        if their device exposes configuration."""
