"""Base camera interface."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from autoweaver.sensor.base import Sensor


@dataclass
class CameraConfig:
    """Camera configuration.

    Attributes:
        device_index: Device index (1-based for Daheng cameras).
        device_sn: Device serial number (preferred over index, stable across reboots).
        exposure_auto: Enable auto exposure.
        gain_auto: Enable auto gain.
        exposure_time: Manual exposure time in microseconds.
        gain: Manual gain value.
        white_balance_mode: White balance mode ("auto", "once", "off").
    """
    device_index: int = 1
    device_sn: Optional[str] = None
    exposure_auto: bool = False
    gain_auto: bool = False
    exposure_time: Optional[float] = None
    gain: Optional[float] = None
    white_balance_mode: str = "once"


class CameraBase(Sensor):
    """Abstract camera — a Sensor whose snapshot is a BGR frame.

    Camera implementations satisfy the Sensor protocol via:
      - ``open / close / is_open``
      - ``snapshot()`` returns ``np.ndarray`` (BGR)
      - ``configure(**kwargs)`` for runtime parameters

    A ``capture()`` alias is provided for backward compatibility with
    code from before 0.5.0; new code should use ``snapshot()``.
    """

    @property
    def name(self) -> str:
        """Default name; subclasses can override."""
        return self.__class__.__name__

    @abstractmethod
    def open(self) -> bool:  # type: ignore[override]
        """Open the camera device.

        Returns True on success. Implementations may raise on hard errors.
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the camera device and release resources."""
        ...

    @abstractmethod
    def snapshot(self) -> np.ndarray:
        """Capture a single BGR frame, shape ``(H, W, 3)`` or ``(H, W)``.

        Raises ``RuntimeError`` if the camera is not opened or capture
        fails.
        """
        ...

    @abstractmethod
    def is_open(self) -> bool:
        """Check whether the camera is opened and ready."""
        ...

    @abstractmethod
    def get_frame_size(self) -> Tuple[int, int]:
        """Return ``(width, height)`` in pixels.

        Raises ``RuntimeError`` if the camera is not opened.
        """
        ...

    @abstractmethod
    def set_exposure_time(self, exposure_time: float) -> None:
        """Set camera exposure time in microseconds."""
        ...

    @abstractmethod
    def set_gain(self, gain: float) -> None:
        """Set camera gain."""
        ...

    # ----- Backward compat aliases -----

    def capture(self) -> np.ndarray:
        """Backward-compat alias for ``snapshot()``.

        New code should call ``snapshot()`` directly to align with the
        Sensor protocol.
        """
        return self.snapshot()

    def is_opened(self) -> bool:
        """Backward-compat alias for ``is_open()``."""
        return self.is_open()

    # ----- Context manager -----

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
