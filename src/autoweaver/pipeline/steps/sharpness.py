"""Sharpness check step for image quality evaluation."""

import logging
from typing import Any, Dict, Optional

import cv2
import numpy as np

from .base import ProcessStep
from ..types import PipelineContext

logger = logging.getLogger(__name__)


class SharpnessCheckStep(ProcessStep):
    """Evaluate image sharpness using Laplacian variance.

    Writes the sharpness score to ctx.metadata["sharpness"].
    Does not modify the image.

    Args:
        params: Step parameters.
            - max_size: Downscale long edge to this before computing (default: 320).
                Speeds up computation on large images.

    Example:
        >>> step = SharpnessCheckStep({"max_size": 320})
        >>> ctx = step.process(ctx)
        >>> print(ctx.metadata["sharpness"])  # e.g. 152.3
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        super().__init__(params)
        self._max_size = self.params.get("max_size", 320)

    def process(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.processed_image is None:
            raise RuntimeError("No image in context")

        gray = cv2.cvtColor(ctx.processed_image, cv2.COLOR_BGR2GRAY)

        # Downscale for speed
        h, w = gray.shape
        scale = self._max_size / max(1.0, float(max(h, w)))
        if scale < 1.0:
            gray = cv2.resize(
                gray,
                (int(w * scale), int(h * scale)),
                interpolation=cv2.INTER_AREA,
            )

        score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        ctx.metadata["sharpness"] = score

        logger.debug(f"Sharpness score: {score:.1f}")
        return ctx
