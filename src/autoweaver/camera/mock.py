"""Mock camera for testing without hardware."""

import logging
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .base import CameraBase, CameraConfig

logger = logging.getLogger(__name__)


class MockCamera(CameraBase):
    """Mock camera implementation for testing.
    
    Can generate random images or cycle through a directory of images.
    Useful for development and testing without actual camera hardware.
    
    Example:
        >>> # Generate random images
        >>> config = CameraConfig()
        >>> camera = MockCamera(config, mode="random")
        
        >>> # Use images from directory
        >>> camera = MockCamera(config, mode="directory", image_dir="test_images/")
    """

    def __init__(
        self,
        config: CameraConfig,
        mode: str = "random",
        image_dir: Optional[str] = None,
        width: int = 640,
        height: int = 480,
    ):
        """Initialize mock camera.
        
        Args:
            config: Camera configuration (mostly ignored).
            mode: "random" for random images, "directory" for file-based.
            image_dir: Directory containing test images (for directory mode).
            width: Image width for random mode.
            height: Image height for random mode.
        """
        self.config = config
        self.mode = mode
        self.image_dir = Path(image_dir) if image_dir else None
        self.width = width
        self.height = height
        
        self._is_opened = False
        self._image_files: List[Path] = []
        self._image_index = 0

    def open(self) -> bool:
        """Open the mock camera."""
        if self.mode == "directory" and self.image_dir:
            if not self.image_dir.exists():
                raise RuntimeError(f"Image directory not found: {self.image_dir}")
            
            # Find all image files
            extensions = [".jpg", ".jpeg", ".png", ".bmp"]
            self._image_files = [
                f for f in self.image_dir.iterdir()
                if f.suffix.lower() in extensions
            ]
            self._image_files.sort()
            
            if not self._image_files:
                raise RuntimeError(f"No images found in {self.image_dir}")
            
            logger.info(f"MockCamera: found {len(self._image_files)} images")
        
        self._is_opened = True
        logger.info(f"MockCamera opened in {self.mode} mode")
        return True

    def close(self) -> None:
        """Close the mock camera."""
        self._is_opened = False
        logger.info("MockCamera closed")

    def capture(self) -> np.ndarray:
        """Capture a frame.
        
        Returns:
            Randomly generated or file-based image.
        """
        if not self._is_opened:
            raise RuntimeError("Camera not opened")
        
        if self.mode == "directory" and self._image_files:
            # Cycle through images
            image_path = self._image_files[self._image_index]
            self._image_index = (self._image_index + 1) % len(self._image_files)
            
            image = cv2.imread(str(image_path))
            if image is None:
                raise RuntimeError(f"Failed to read image: {image_path}")
            
            return image
        
        else:
            # Generate random image
            return np.random.randint(
                0, 256,
                (self.height, self.width, 3),
                dtype=np.uint8
            )

    def is_opened(self) -> bool:
        """Check if camera is opened."""
        return self._is_opened

    def get_frame_size(self) -> Tuple[int, int]:
        """Get frame size."""
        if not self._is_opened:
            raise RuntimeError("Camera not opened")
        
        if self.mode == "directory" and self._image_files:
            # Read first image to get size
            image = cv2.imread(str(self._image_files[0]))
            if image is not None:
                return (image.shape[1], image.shape[0])
        
        return (self.width, self.height)


