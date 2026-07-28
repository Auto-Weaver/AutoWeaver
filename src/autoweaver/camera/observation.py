"""CameraObservation — an Observation whose payload is an image.

See EVO-011. Adds the two reframing operations that actually occur in practice
(crop to an ROI, downscale for display) and, crucially, makes them **traceable**:
each returns a new observation that remembers how to map its own pixels back.

Why that matters: today, cropping to an ROI silently changes what a pixel
coordinate means, and it becomes the business code's job to remember to add the
origin back. Forgetting does not raise — it just makes every result wrong by the
offset. With lineage, the derived observation converts its own coordinates
(:meth:`Observation.to_root`), so nothing has to be remembered.

``crop`` returns a **view**: no pixels are copied. A full-resolution frame is
about 9 MB, so a container that copies on every step is not affordable — that is
the very habit this replaces. ``resize`` must allocate; that is inherent to
resampling, not a design choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

from autoweaver.sensor.observation import Observation, PixelTransform


@dataclass(frozen=True)
class CameraObservation(Observation):
    """An :class:`Observation` carrying a BGR (or single-channel) image.

    ``data`` is an ``np.ndarray`` of shape ``(H, W, 3)`` or ``(H, W)``, marked
    read-only when the observation is constructed.
    """

    @property
    def image(self) -> np.ndarray:
        """The payload, named for what it is."""
        return self.data

    @property
    def height(self) -> int:
        return int(self.data.shape[0])

    @property
    def width(self) -> int:
        return int(self.data.shape[1])

    @property
    def size(self) -> Tuple[int, int]:
        """``(width, height)`` in pixels."""
        return (self.width, self.height)

    # -- reframing --------------------------------------------------------- #

    def crop(self, x: int, y: int, width: int, height: int) -> "CameraObservation":
        """Crop to ``(x, y, width, height)``, returning a **view** with lineage.

        Bounds are validated rather than clamped: a crop running off the edge is
        a caller bug, and quietly returning a smaller region would corrupt every
        coordinate derived from it.
        """
        x, y, width, height = int(x), int(y), int(width), int(height)
        if width <= 0 or height <= 0:
            raise ValueError(f"crop size must be positive, got {width}x{height}")
        if x < 0 or y < 0 or x + width > self.width or y + height > self.height:
            raise ValueError(
                f"crop ({x}, {y}, {width}, {height}) falls outside "
                f"{self.width}x{self.height}"
            )
        view = self.data[y : y + height, x : x + width]
        return self.derive(view, kind="crop", transform=PixelTransform.crop(x, y))

    def resize(
        self,
        *,
        scale: Optional[float] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        interpolation: int = cv2.INTER_LINEAR,
    ) -> "CameraObservation":
        """Resample, returning a new observation with lineage.

        Give either ``scale`` or at least one of ``width`` / ``height`` (the
        missing one keeps the aspect ratio). Unlike :meth:`crop` this allocates.
        """
        if scale is not None and (width is not None or height is not None):
            raise ValueError("give either scale or width/height, not both")
        if scale is not None:
            if scale <= 0:
                raise ValueError(f"scale must be positive, got {scale}")
            new_width = max(1, int(round(self.width * scale)))
            new_height = max(1, int(round(self.height * scale)))
        elif width is not None or height is not None:
            if width is not None and height is not None:
                new_width, new_height = int(width), int(height)
            elif width is not None:
                new_width = int(width)
                new_height = max(1, int(round(self.height * (new_width / self.width))))
            else:
                new_height = int(height)  # type: ignore[arg-type]
                new_width = max(1, int(round(self.width * (new_height / self.height))))
            if new_width <= 0 or new_height <= 0:
                raise ValueError(f"resize target must be positive, got {new_width}x{new_height}")
        else:
            raise ValueError("resize needs scale, width or height")

        resized = cv2.resize(
            self.data, (new_width, new_height), interpolation=interpolation
        )
        transform = PixelTransform.resize(
            new_width / self.width, new_height / self.height
        )
        return self.derive(resized, kind="resize", transform=transform)


__all__ = ["CameraObservation"]
