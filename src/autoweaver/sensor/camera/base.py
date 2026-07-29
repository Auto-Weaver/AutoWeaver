"""Base camera interface."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

import numpy as np

from autoweaver.sensor.camera.observation import CameraObservation
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

            .. warning::
               In YAML, bare ``off`` parses as the boolean ``False``, not the
               string ``"off"``. Quote it (``white_balance_mode: "off"``).
        white_balance_red: Manual white-balance gain for the RED channel.
        white_balance_green: Manual white-balance gain for the GREEN channel.
        white_balance_blue: Manual white-balance gain for the BLUE channel.

            The three ratios are ONE measurement, so they are given together or
            not at all — a partial set is a configuration error, not something
            to fill in with 1.0. They are written to the device after the mode,
            and only mean anything under ``white_balance_mode="off"``: any
            automatic mode ("auto"/"continuous", and "once" at open) computes
            its own ratios and overwrites whatever was written here.

            Why hard-code them at all: the ratios multiply straight into the
            grayscale a detector thresholds against, so a re-converged auto
            white balance silently shifts every absolute-gray gate, and a
            two-frame differential judge sees a global residual that no physical
            motion produced. Measure once on the rig, then freeze.
        trigger_mode: Acquisition mode. False (default) = continuous free-run —
            the camera streams at its frame rate and ``snapshot()`` returns the
            next buffered frame (which can be stale if the consumer lags the
            frame rate). True = software trigger (capture-on-demand): the camera
            produces a frame only when ``snapshot()`` fires the trigger, so every
            grab is freshly acquired. Prefer trigger mode for move-then-capture
            workflows (e.g. calibration).
    """
    device_index: int = 1
    device_sn: Optional[str] = None
    exposure_auto: bool = False
    gain_auto: bool = False
    exposure_time: Optional[float] = None
    gain: Optional[float] = None
    white_balance_mode: str = "once"
    white_balance_red: Optional[float] = None
    white_balance_green: Optional[float] = None
    white_balance_blue: Optional[float] = None
    trigger_mode: bool = False


class CameraBase(Sensor):
    """Abstract camera — a Sensor whose snapshot is a BGR frame.

    Camera implementations satisfy the Sensor protocol via:
      - ``open / close / is_open``
      - ``snapshot()`` returns ``np.ndarray`` (BGR)
      - ``configure(**kwargs)`` for runtime parameters

    ``observe()`` (EVO-011) wraps that frame in a :class:`CameraObservation`,
    stamped with the identity, capture instant and imaging conditions the device
    alone knows. It requires :attr:`Sensor.role` to be set.

    A ``capture()`` alias is provided for backward compatibility with
    code from before 0.5.0; new code should use ``snapshot()``.

    .. warning::
       ``capture()`` delegates **to** ``snapshot()``. A subclass that customises
       acquisition must therefore override ``snapshot()``; overriding only
       ``capture()`` leaves ``observe()`` — and anything else on the Sensor
       contract — bypassing the customisation without any error.
    """

    #: Fields of ``CameraConfig`` that describe how the frame was acquired.
    #: These travel with every observation because nothing downstream can
    #: recover them, yet detection thresholds are calibrated against them.
    _CONDITION_FIELDS = (
        "exposure_time",
        "exposure_auto",
        "gain",
        "gain_auto",
        "white_balance_mode",
        "white_balance_red",
        "white_balance_green",
        "white_balance_blue",
        "trigger_mode",
    )

    @property
    def name(self) -> str:
        """Default name; subclasses can override."""
        return self.__class__.__name__

    # -- observation ------------------------------------------------------- #

    def _observation_conditions(self) -> Mapping[str, Any]:
        """Imaging conditions read off this camera's config.

        Returns an empty mapping when the subclass keeps no ``config`` — the
        contract is "report what the device knows", not "invent defaults".
        """
        config = getattr(self, "config", None)
        if config is None:
            return {}
        conditions = {}
        for field_name in self._CONDITION_FIELDS:
            if hasattr(config, field_name):
                conditions[field_name] = getattr(config, field_name)
        return conditions

    def _build_observation(
        self, *, observation_id: int, source: str, captured_at: float, payload: Any
    ) -> CameraObservation:
        return CameraObservation(
            id=observation_id,
            source=source,
            captured_at=captured_at,
            data=payload,
            conditions=self._observation_conditions(),
            projection=self.projection,
        )

    def observe(self) -> CameraObservation:
        """One frame, wrapped as a :class:`CameraObservation`. See :meth:`Sensor.observe`."""
        return super().observe()  # type: ignore[return-value]

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
