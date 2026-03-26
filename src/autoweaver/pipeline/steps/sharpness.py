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
            - center_ratio: Fraction of the image (per axis) to keep as a
                center crop before scoring (default: None = full image).
                For example, 0.25 on a 5120x5120 image crops to 1280x1280.

    Example:
        >>> step = SharpnessCheckStep({"center_ratio": 0.25})
        >>> ctx = step.process(ctx)
        >>> print(ctx.metadata["sharpness"])  # e.g. 152.3
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        super().__init__(params)
        self._center_ratio: Optional[float] = self.params.get("center_ratio")

    def process(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.processed_image is None:
            raise RuntimeError("No image in context")

        gray = cv2.cvtColor(ctx.processed_image, cv2.COLOR_BGR2GRAY)

        # Center crop
        if self._center_ratio is not None and 0.0 < self._center_ratio < 1.0:
            h, w = gray.shape
            ch, cw = int(h * self._center_ratio), int(w * self._center_ratio)
            y0 = (h - ch) // 2
            x0 = (w - cw) // 2
            gray = gray[y0 : y0 + ch, x0 : x0 + cw]

        score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        ctx.metadata["sharpness"] = score

        logger.debug("Sharpness score: %.1f", score)
        return ctx
