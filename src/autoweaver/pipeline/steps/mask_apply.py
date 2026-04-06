"""Apply segmentation mask to image — fill outside mask, crop to bounding box."""

import logging
from typing import List, Optional

import cv2
import numpy as np

from ..types import PipelineContext
from .base import ProcessStep

logger = logging.getLogger(__name__)


class MaskApplyStep(ProcessStep):
    """Apply a segmentation mask to the processed image.

    Reads ``ctx.metadata["segments"]`` (produced by YOLOSegStep or any
    source that writes :class:`SegmentResult` objects), selects one
    segment, fills pixels outside the mask, and crops to the mask's
    bounding box.

    The result replaces ``ctx.processed_image``.

    Parameters:
        padding: Pixels to add around the mask bounding box (default 0).
        fill_value: BGR fill for pixels outside the mask (default [0, 0, 0]).
        select_by: Strategy to pick one segment (default "area").
            "area" — largest mask area.
            "confidence" — highest detection confidence.
            "center" — closest to image center.
        segment_index: If set, directly pick this index (overrides select_by).
    """

    def __init__(self, params: dict = None):
        super().__init__(params)

        self.padding: int = self.params.get("padding", 0)
        self.fill_value = self.params.get("fill_value", [0, 0, 0])
        self.select_by: str = self.params.get("select_by", "area")
        self.segment_index: Optional[int] = self.params.get("segment_index")

    @property
    def name(self) -> str:
        return self._custom_name or "mask_apply"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        segments = ctx.metadata.get("segments")
        if not segments:
            raise ValueError(
                "MaskApplyStep requires ctx.metadata['segments'] "
                "(produced by YOLOSegStep)"
            )

        image = ctx.processed_image
        if image is None:
            raise ValueError("MaskApplyStep requires ctx.processed_image")

        segment = self._select_segment(segments, image.shape[:2])
        if segment is None:
            raise ValueError("No valid segment found")

        # Apply mask: fill outside, crop to bbox
        masked = image.copy()
        masked[segment.mask == 0] = self.fill_value

        # Crop to bbox with padding
        h, w = image.shape[:2]
        x1, y1 = int(segment.bbox.x1), int(segment.bbox.y1)
        x2, y2 = int(segment.bbox.x2) + 1, int(segment.bbox.y2) + 1

        x1 = max(0, x1 - self.padding)
        y1 = max(0, y1 - self.padding)
        x2 = min(w, x2 + self.padding)
        y2 = min(h, y2 + self.padding)

        ctx.processed_image = masked[y1:y2, x1:x2].copy()

        ctx.metadata["mask_apply"] = {
            "selected_class": segment.class_name,
            "selected_confidence": segment.confidence,
            "bbox": segment.bbox.to_dict(),
            "cropped_shape": list(ctx.processed_image.shape[:2]),
            "mask_area": int(np.count_nonzero(segment.mask)),
        }

        logger.debug(
            "MaskApply: selected %s (conf=%.2f) -> %dx%d",
            segment.class_name,
            segment.confidence,
            ctx.processed_image.shape[1],
            ctx.processed_image.shape[0],
        )
        return ctx

    def _select_segment(self, segments, image_shape):
        """Pick one segment based on the selection strategy."""
        if not segments:
            return None

        if self.segment_index is not None:
            idx = self.segment_index
            if 0 <= idx < len(segments):
                return segments[idx]
            return None

        if self.select_by == "confidence":
            return max(segments, key=lambda s: s.confidence)

        if self.select_by == "center":
            h, w = image_shape
            cx, cy = w / 2, h / 2
            return min(
                segments,
                key=lambda s: (s.bbox.center[0] - cx) ** 2
                + (s.bbox.center[1] - cy) ** 2,
            )

        # Default: area
        return max(segments, key=lambda s: np.count_nonzero(s.mask))
