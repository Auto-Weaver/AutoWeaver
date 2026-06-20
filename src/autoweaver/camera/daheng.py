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

    def _configure_white_balance(self) -> None:
        """Configure white balance."""
        gx = self._gx
        mode = (self.config.white_balance_mode or "auto").lower()
        
        mode_map = {
            "off": gx.GxAutoEntry.OFF,
            "auto": gx.GxAutoEntry.CONTINUOUS,
            "continuous": gx.GxAutoEntry.CONTINUOUS,
            "once": gx.GxAutoEntry.ONCE,
        }
        
        if mode in mode_map and hasattr(self._cam, "BalanceWhiteAuto"):
            try:
                self._cam.BalanceWhiteAuto.set(mode_map[mode])
                logger.debug(f"BalanceWhiteAuto set to {mode}")
            except Exception as e:
                logger.warning(f"Failed to set white balance: {e}")

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
