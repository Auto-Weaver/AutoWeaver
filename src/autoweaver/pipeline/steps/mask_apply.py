"""Apply segmentation mask to image — fill outside mask, crop to bounding box."""

import logging
from typing import Optional

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

    When ``auto_rotate`` is enabled, the mask's minimum-area rotated
    rectangle is used to find the edge closest to vertical (the y-axis).
    If that edge deviates by no more than ``max_rotation`` degrees,
    the image is rotated to align it perfectly vertical.

    The result replaces ``ctx.processed_image``.

    Parameters:
        padding: Pixels to add around the mask bounding box (default 0).
        fill_value: BGR fill for pixels outside the mask (default [0, 0, 0]).
        select_by: Strategy to pick one segment (default "area").
            "area" — largest mask area.
            "confidence" — highest detection confidence.
            "center" — closest to image center.
        segment_index: If set, directly pick this index (overrides select_by).
        auto_rotate: Rotate the image to align the mask region upright
            (default False).
        max_rotation: Maximum rotation angle in degrees (default 30).
            If the computed angle exceeds this, rotation is skipped.
    """

    def __init__(self, params: dict = None):
        super().__init__(params)

        self.padding: int = self.params.get("padding", 0)
        self.fill_value = self.params.get("fill_value", [0, 0, 0])
        self.select_by: str = self.params.get("select_by", "area")
        self.segment_index: Optional[int] = self.params.get("segment_index")
        self.auto_rotate: bool = self.params.get("auto_rotate", False)
        self.max_rotation: float = self.params.get("max_rotation", 30.0)

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

        mask = segment.mask
        rotation_angle = 0.0

        if self.auto_rotate:
            angle = self._compute_vertical_angle(mask)
            if angle is not None and abs(angle) <= self.max_rotation:
                rotation_angle = angle
                image, mask = self._rotate_image_and_mask(image, mask, angle)
            elif angle is not None:
                logger.warning(
                    "Rotation angle %.1f° exceeds max_rotation %.1f°, skipping",
                    angle, self.max_rotation,
                )

        # Apply mask: fill outside
        masked = image.copy()
        masked[mask == 0] = self.fill_value

        # Crop to mask bbox with padding
        ys, xs = np.where(mask > 0)
        if len(ys) == 0:
            raise ValueError("Empty mask after processing")

        h, w = image.shape[:2]
        y1, y2 = int(ys.min()), int(ys.max()) + 1
        x1, x2 = int(xs.min()), int(xs.max()) + 1

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
            "rotation_angle": rotation_angle,
        }

        logger.debug(
            "MaskApply: selected %s (conf=%.2f, rot=%.1f°) -> %dx%d",
            segment.class_name,
            segment.confidence,
            rotation_angle,
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

    @staticmethod
    def _compute_vertical_angle(mask: np.ndarray) -> Optional[float]:
        """Find the angle of the edge closest to vertical (y-axis).

        Computes the four edges of the mask's minAreaRect, measures
        each edge's angle to the y-axis (downward), and returns the
        smallest such angle as the correction needed.

        Returns:
            Rotation angle in degrees to align the closest-to-vertical
            edge with the y-axis, or None if no contour is found.
            Positive = clockwise correction needed.
        """
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            return None

        largest = max(contours, key=cv2.contourArea)
        rect = cv2.minAreaRect(largest)
        box = cv2.boxPoints(rect)  # 4 corner points

        # Compute the 4 edge vectors
        best_angle = None
        for i in range(4):
            p1 = box[i]
            p2 = box[(i + 1) % 4]
            dx = p2[0] - p1[0]
            dy = p2[1] - p1[1]

            # Angle between this edge and the y-axis (downward = (0, 1))
            # Using atan2: angle of edge vector relative to positive x-axis
            edge_angle = np.degrees(np.arctan2(dy, dx))

            # Deviation from vertical (90° or -90° from x-axis)
            # Vertical down is 90°, vertical up is -90°
            dev_from_down = abs(edge_angle - 90.0)
            dev_from_up = abs(edge_angle + 90.0)
            deviation = min(dev_from_down, dev_from_up)

            if best_angle is None or deviation < abs(best_angle):
                # The correction angle: how much to rotate so this edge
                # becomes perfectly vertical.
                # If edge is at 85° (close to 90° down), correction is -5°
                # If edge is at -87° (close to -90° up), correction is +3°
                if dev_from_down <= dev_from_up:
                    best_angle = edge_angle - 90.0
                else:
                    best_angle = edge_angle + 90.0

        return best_angle

    @staticmethod
    def _rotate_image_and_mask(
        image: np.ndarray, mask: np.ndarray, angle: float
    ) -> tuple:
        """Rotate image and mask to correct the given angle.

        The output canvas is expanded so no content is clipped.
        """
        h, w = image.shape[:2]
        cx, cy = w / 2, h / 2

        M = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)

        # Expand canvas to avoid clipping
        cos = abs(M[0, 0])
        sin = abs(M[0, 1])
        new_w = int(h * sin + w * cos)
        new_h = int(h * cos + w * sin)

        M[0, 2] += (new_w - w) / 2
        M[1, 2] += (new_h - h) / 2

        rotated_image = cv2.warpAffine(
            image, M, (new_w, new_h),
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0),
        )
        rotated_mask = cv2.warpAffine(
            mask, M, (new_w, new_h),
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        # Re-binarize after interpolation
        rotated_mask = (rotated_mask > 127).astype(np.uint8) * 255

        return rotated_image, rotated_mask
