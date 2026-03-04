"""Base camera interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np


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


class CameraBase(ABC):
    """Abstract base class for cameras.
    
    This interface allows for different camera implementations
    while keeping the rest of the system camera-agnostic.
    """

    @abstractmethod
    def open(self) -> bool:
        """Open the camera device.
        
        Returns:
            True if opened successfully, False otherwise.
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """Close the camera device and release resources."""
        pass

    @abstractmethod
    def capture(self) -> np.ndarray:
        """Capture a single frame.
        
        Returns:
            Image as numpy array (H, W, C) in BGR format.
        
        Raises:
            RuntimeError: If capture fails or camera not opened.
        """
        pass

    @abstractmethod
    def is_opened(self) -> bool:
        """Check if camera is opened.
        
        Returns:
            True if camera is opened and ready.
        """
        pass

    @abstractmethod
    def get_frame_size(self) -> Tuple[int, int]:
        """Get frame size.
        
        Returns:
            Tuple of (width, height) in pixels.
            
        Raises:
            RuntimeError: If camera not opened.
        """
        pass

    def __enter__(self):
        """Context manager entry."""
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False
