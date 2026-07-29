"""Daheng Imaging camera implementation using gxipy SDK."""

import logging
from typing import Tuple

import numpy as np

from .base import CameraBase, CameraConfig

try:
    import gxipy as gx
except ImportError:
    gx = None

logger = logging.getLogger(__name__)


class DahengCamera(CameraBase):
    """Daheng Imaging camera implementation.
    
    Example:
        >>> config = CameraConfig(device_index=1)
        >>> with DahengCamera(config) as camera:
        ...     image = camera.capture()
    """

    def __init__(self, config: CameraConfig):
        if gx is None:
            raise ImportError(
                "DahengCamera requires the Galaxy SDK. "
                "Install the Daheng Galaxy SDK and ensure gxipy is available."
            )
        self.config = config
        self._dev_mgr = None
        self._cam = None
        self._is_opened = False
        self._gx = gx

    def open(self) -> bool:
        """Open the camera device."""
        try:
            self._dev_mgr = gx.DeviceManager()
            num, _ = self._dev_mgr.update_device_list()
            
            if num == 0:
                raise RuntimeError("No Daheng camera found")

            logger.info(f"Found {num} Daheng camera(s)")

            if self.config.device_sn:
                self._cam = self._dev_mgr.open_device_by_sn(self.config.device_sn)
                logger.info(f"Opened camera by SN: {self.config.device_sn}")
            else:
                self._cam = self._dev_mgr.open_device_by_index(self.config.device_index)
                logger.info(f"Opened camera by index: {self.config.device_index}")
            self._configure_camera()
            self._cam.stream_on()
            self._is_opened = True
            
            width, height = self.get_frame_size()
            logger.info(f"Camera opened: {width}x{height}")
            return True
            
        except Exception as e:
            self._is_opened = False
            logger.error(f"Failed to open camera: {e}")
            raise RuntimeError(f"Failed to open camera: {e}")

    def _configure_camera(self) -> None:
        """Configure camera parameters."""
        cam = self._cam
        cfg = self.config
        
        # Exposure
        cam.ExposureAuto.set(cfg.exposure_auto)
        if cfg.exposure_time is not None:
            cam.ExposureTime.set(cfg.exposure_time)
        
        # Gain
        cam.GainAuto.set(cfg.gain_auto)
        if cfg.gain is not None:
            cam.Gain.set(cfg.gain)
        
        # White balance
        self._configure_white_balance()

        # Acquisition mode (continuous free-run vs software trigger)
        self._configure_trigger()

    def _configure_trigger(self) -> None:
        """Set continuous free-run (default) or software-trigger acquisition.

        Software trigger makes the camera produce a frame only on demand, so each
        ``snapshot()`` returns a freshly-acquired frame instead of the next one
        buffered off the continuous stream — no stale frames when the consumer
        lags the frame rate. Configured here (stream still off); ``snapshot()``
        fires the trigger per grab."""
        gx = self._gx
        cam = self._cam
        try:
            if self.config.trigger_mode:
                cam.TriggerMode.set(gx.GxSwitchEntry.ON)
                cam.TriggerSource.set(gx.GxTriggerSourceEntry.SOFTWARE)
                logger.info("Acquisition mode: software trigger (capture-on-demand)")
            else:
                cam.TriggerMode.set(gx.GxSwitchEntry.OFF)
                logger.debug("Acquisition mode: continuous (free-run)")
        except Exception as e:
            logger.warning(f"Failed to set trigger mode: {e}")

    #: Quantisation step of ``BalanceRatio`` on Daheng devices (1/512). Read-back
    #: comparison must tolerate it; exact float equality never holds.
    _BALANCE_RATIO_STEP = 1.0 / 512.0

    @staticmethod
    def _feature_available(cam, name: str, *, writable: bool = True) -> bool:
        """Whether ``cam.<name>`` really exists on THIS device.

        ``hasattr`` is useless here: gxipy's ``Device.__init__`` attaches every
        feature object unconditionally, so ``hasattr(cam, "BalanceWhiteAuto")`` is
        True even on a mono camera. The device is only queried by
        ``is_implemented`` / ``is_writable`` on the ``Feature`` base class.
        """
        feature = getattr(cam, name, None)
        if feature is None:
            return False
        try:
            return bool(feature.is_writable() if writable else feature.is_readable())
        except Exception:  # noqa: BLE001 — SDK raises on some access paths
            return False

    def _white_balance_ratios(self):
        """The configured (red, green, blue) manual gains, or ``None`` if unset.

        Raises ``ValueError`` on a partial set: white balance is one measurement
        of three numbers, so one or two of them is a config error, not an
        invitation to guess the rest.
        """
        cfg = self.config
        ratios = {
            "RED": getattr(cfg, "white_balance_red", None),
            "GREEN": getattr(cfg, "white_balance_green", None),
            "BLUE": getattr(cfg, "white_balance_blue", None),
        }
        given = {k: v for k, v in ratios.items() if v is not None}
        if not given:
            return None
        if len(given) != 3:
            missing = sorted(k for k, v in ratios.items() if v is None)
            raise ValueError(
                "white balance ratios are a set of three: got "
                f"{ {k: float(v) for k, v in given.items()} } but "
                f"{', '.join(missing)} is unset. Give all of "
                "white_balance_red / white_balance_green / white_balance_blue, "
                "or none of them — a partial set has no physical meaning."
            )
        return {k: float(v) for k, v in ratios.items()}

    def _configure_white_balance(self) -> None:
        """Set the white-balance mode, then any hard-coded manual ratios.

        The ratios are written after the mode on purpose: an automatic mode
        recomputes them and would overwrite an earlier write.
        """
        gx = self._gx
        raw_mode = self.config.white_balance_mode

        # A non-string mode is ALWAYS a bug, and the old `or "auto"` fallback made
        # it a silent one: YAML parses a bare `off` as the boolean False, which fell
        # through to CONTINUOUS — i.e. white balance running non-stop, the exact
        # opposite of what was asked, with nothing logged. Fail loud instead.
        if not isinstance(raw_mode, str):
            raise TypeError(
                f"white_balance_mode must be a string, got {raw_mode!r} "
                f"({type(raw_mode).__name__}). In YAML a bare `off` parses as the "
                f'boolean False — write white_balance_mode: "off" with quotes.'
            )

        mode = raw_mode.lower()
        mode_map = {
            "off": gx.GxAutoEntry.OFF,
            "auto": gx.GxAutoEntry.CONTINUOUS,
            "continuous": gx.GxAutoEntry.CONTINUOUS,
            "once": gx.GxAutoEntry.ONCE,
        }
        if mode not in mode_map:
            raise ValueError(
                f"unknown white_balance_mode {raw_mode!r}; "
                f"expected one of {sorted(mode_map)}"
            )

        # Raise on a partial ratio set BEFORE touching the device, so a config
        # error cannot leave the camera half-configured.
        ratios = self._white_balance_ratios()

        if self._feature_available(self._cam, "BalanceWhiteAuto"):
            try:
                self._cam.BalanceWhiteAuto.set(mode_map[mode])
                logger.debug(f"BalanceWhiteAuto set to {mode}")
            except Exception as e:
                logger.warning(f"Failed to set white balance mode {mode!r}: {e}")
        else:
            logger.warning(
                "white balance mode %r NOT applied — feature 'BalanceWhiteAuto' is "
                "not implemented/writable on this device", mode)

        if ratios is None:
            return

        if mode != "off":
            logger.warning(
                "white_balance_mode=%r together with manual ratios %s — automatic "
                "white balance will recompute and overwrite them. Set "
                'white_balance_mode: "off" if the measured ratios are meant to hold.',
                mode, ratios)

        self._write_white_balance_ratios(ratios)

    def _write_white_balance_ratios(self, ratios) -> None:
        """Write the three manual gains, then read them back and verify.

        ``BalanceRatio`` is ONE selector-driven register, not three features: each
        channel is a "select the channel, then write the value" pair.
        """
        gx = self._gx
        cam = self._cam

        for feature_name in ("BalanceRatioSelector", "BalanceRatio"):
            if not self._feature_available(cam, feature_name):
                logger.warning(
                    "manual white balance %s NOT applied — feature %r is not "
                    "implemented/writable on this device", ratios, feature_name)
                return

        selector_map = {
            "RED": gx.GxBalanceRatioSelectorEntry.RED,
            "GREEN": gx.GxBalanceRatioSelectorEntry.GREEN,
            "BLUE": gx.GxBalanceRatioSelectorEntry.BLUE,
        }
        tolerance = self._BALANCE_RATIO_STEP * 1.5

        for channel, wanted in ratios.items():
            try:
                cam.BalanceRatioSelector.set(selector_map[channel])
                cam.BalanceRatio.set(wanted)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "failed to set BalanceRatio for %s to %.4f: %s", channel, wanted, e)
                continue
            try:
                actual = float(cam.BalanceRatio.get())
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "BalanceRatio for %s written as %.4f but could not be read back "
                    "for verification: %s", channel, wanted, e)
                continue
            if abs(actual - wanted) > tolerance:
                logger.warning(
                    "BalanceRatio %s read back as %.4f after writing %.4f "
                    "(tolerance %.4f) — the device did not take the value",
                    channel, actual, wanted, tolerance)
            else:
                logger.debug("BalanceRatio %s = %.4f (wanted %.4f)", channel, actual, wanted)

        logger.info("manual white balance applied: %s", ratios)

    def close(self) -> None:
        """Close the camera device."""
        if self._cam is not None:
            try:
                self._cam.stream_off()
                self._cam.close_device()
                logger.info("Camera closed")
            except Exception as e:
                logger.warning(f"Error closing camera: {e}")
            finally:
                self._cam = None
        self._is_opened = False

    def snapshot(self) -> np.ndarray:
        """Capture a single frame in BGR format."""
        if not self._is_opened:
            raise RuntimeError("Camera not opened")

        gx = self._gx
        # In software-trigger mode, fire one trigger so the camera acquires a
        # fresh frame now (continuous mode just reads the next streamed frame).
        if self.config.trigger_mode:
            self._cam.TriggerSoftware.send_command()
        raw_image = self._cam.data_stream[0].get_image()
        
        if raw_image is None:
            raise RuntimeError("Failed to capture image")
        
        if raw_image.get_status() != gx.GxFrameStatusList.SUCCESS:
            raise RuntimeError("Frame capture failed: incomplete frame")
        
        # Convert to BGR (handles Bayer, Mono, RGB, etc.)
        rgb_image = raw_image.convert("RGB", channel_order=gx.DxRGBChannelOrder.ORDER_BGR)
        if rgb_image is None:
            raise RuntimeError("Failed to convert image to BGR")
        
        image = rgb_image.get_numpy_array()
        if image is None:
            raise RuntimeError("Failed to get numpy array")
        
        return image

    def is_open(self) -> bool:
        """Check if camera is opened."""
        return self._is_opened

    def get_frame_size(self) -> Tuple[int, int]:
        """Get frame size (width, height)."""
        if not self._is_opened:
            raise RuntimeError("Camera not opened")
        return (self._cam.Width.get(), self._cam.Height.get())

    def set_exposure_time(self, exposure_time: float) -> None:
        """Set exposure time in microseconds."""
        if not self._is_opened:
            raise RuntimeError("Camera not opened")
        self._cam.ExposureTime.set(exposure_time)
        logger.debug(f"Exposure time set to {exposure_time} us")

    def set_gain(self, gain: float) -> None:
        """Set gain value."""
        if not self._is_opened:
            raise RuntimeError("Camera not opened")
        self._cam.Gain.set(gain)
        logger.debug(f"Gain set to {gain}")
