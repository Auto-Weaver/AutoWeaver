"""Vision module data types."""

from dataclasses import dataclass, field
from typing import Any, Dict, Generic, List, Optional, TypeVar

import numpy as np

# Payload type carried by a pipeline run. The container makes **no**
# assumptions about it — steps that need to look inside a payload declare
# the shape they require via a Protocol next to themselves (see
# ``steps/postprocess.py``'s ``BoxLike``). ``Detection`` /
# ``RegionDetection`` below are convenience payloads, not a mandated floor.
D = TypeVar("D")


@dataclass
class BoundingBox:
    """Bounding box in pixel coordinates.
    
    Uses (x1, y1, x2, y2) format where:
    - (x1, y1) is the top-left corner
    - (x2, y2) is the bottom-right corner
    
    Attributes:
        x1: Left edge x coordinate.
        y1: Top edge y coordinate.
        x2: Right edge x coordinate.
        y2: Bottom edge y coordinate.
    """
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        """Box width in pixels."""
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        """Box height in pixels."""
        return self.y2 - self.y1

    @property
    def center(self) -> tuple:
        """Box center (cx, cy)."""
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    @property
    def area(self) -> float:
        """Box area in pixels squared."""
        return self.width * self.height

    def to_xyxy(self) -> tuple:
        """Return as (x1, y1, x2, y2) tuple."""
        return (self.x1, self.y1, self.x2, self.y2)

    def to_xywh(self) -> tuple:
        """Return as (x, y, width, height) tuple."""
        return (self.x1, self.y1, self.width, self.height)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
        }


@dataclass
class Detection:
    """Single detection result.
    
    Represents one detected object with its location,
    classification, and confidence score.
    
    Attributes:
        bbox: Bounding box location.
        object_type: Class name from model labels.
        confidence: Detection confidence score (0-1).
        detection_id: Optional unique identifier.
    """
    bbox: BoundingBox
    object_type: str
    confidence: float
    detection_id: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "bbox": self.bbox.to_dict(),
            "object_type": self.object_type,
            "confidence": self.confidence,
            "detection_id": self.detection_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Detection":
        """Create from dictionary."""
        return cls(
            bbox=BoundingBox(**data["bbox"]),
            object_type=data["object_type"],
            confidence=data["confidence"],
            detection_id=data.get("detection_id"),
        )


@dataclass(kw_only=True)
class RegionDetection(Detection):
    """A :class:`Detection` that also carries a pixel mask for its region.

    Convenience payload for segmentation-style results. Like
    :class:`Detection`, it is an optional helper — the pipeline container
    does not require it — but it exactly satisfies the ``BoxLike`` contract
    consumed by the postprocess steps, so it drops into NMS / Filter / Sort
    unchanged.

    The mask is stored in **bbox-local** coordinates: a ``(h, w)`` array
    sized to the bounding box, **not** the full frame. A full-frame mask on
    a 4000×3000 image is ~12 MB each — unacceptable to keep per detection.
    Reconstruct a full-frame mask by pasting ``mask`` at ``(bbox.x1, bbox.y1)``.

    ``kw_only=True`` is required: the base class has a trailing field with a
    default (``detection_id``), and these two fields have none — without
    ``kw_only`` the dataclass would raise "non-default argument follows
    default argument".

    Attributes:
        mask: Bbox-local binary mask, uint8, values 0 or 255, shape (h, w).
        area_px: Cached count of non-zero (foreground) pixels in the mask.
    """

    mask: np.ndarray
    area_px: int

    def to_dict(self) -> dict:
        """Convert to dictionary (raw mask excluded for serialization)."""
        d = super().to_dict()
        d.update(
            {
                "mask_shape": list(self.mask.shape),
                "area_px": self.area_px,
            }
        )
        return d


@dataclass
class PipelineContext(Generic[D]):
    """Context passed between pipeline steps.

    This object carries the image and accumulated results through
    the processing pipeline. Each step can read from and write to
    this context.

    Attributes:
        original_image: The original input image (set by CaptureStep).
        processed_image: The current processed image (may be modified by steps).
        detections: List of payloads accumulated by steps. The container is
            payload-agnostic (``PipelineContext[D]``); ``PipelineContext()``
            with no type argument keeps the old, untyped behaviour.
        metadata: Additional metadata from processing steps.
    """
    original_image: Optional[np.ndarray] = None
    processed_image: Optional[np.ndarray] = None
    detections: List[D] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Initialize processed_image if not provided."""
        if self.processed_image is None and self.original_image is not None:
            self.processed_image = self.original_image.copy()


@dataclass
class PipelineResult(Generic[D]):
    """Final result from vision pipeline.

    Contains all detections and processing information after
    the entire pipeline has completed.

    Attributes:
        detections: List of all detections.
        processing_time_ms: Total processing time in milliseconds.
        metadata: Processing metadata from all steps.
        original_image: The original input image (set by CaptureStep).
        processed_image: The final processed image after all steps.
    """
    detections: List[D]
    processing_time_ms: float
    metadata: Dict[str, Any]
    original_image: Optional[np.ndarray] = None
    processed_image: Optional[np.ndarray] = None

    @property
    def detection_count(self) -> int:
        """Number of detections."""
        return len(self.detections)

    def get_detections_by_type(self, object_type: str) -> List[D]:
        """Get detections filtered by ``object_type``.

        Convenience for payloads that expose an ``object_type`` attribute
        (e.g. :class:`Detection`); not meaningful for payloads that don't.
        """
        return [d for d in self.detections if getattr(d, "object_type", None) == object_type]

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "detections": [d.to_dict() for d in self.detections],
            "detection_count": self.detection_count,
            "processing_time_ms": self.processing_time_ms,
            "metadata": self.metadata,
        }


