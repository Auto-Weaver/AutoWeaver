"""Vision module data types."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


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


@dataclass
class PipelineContext:
    """Context passed between pipeline steps.
    
    This object carries the image and accumulated results through
    the processing pipeline. Each step can read from and write to
    this context.
    
    Attributes:
        original_image: The original input image (unchanged).
        processed_image: The current processed image (may be modified).
        detections: List of detection results.
        metadata: Additional metadata from processing steps.
    """
    original_image: np.ndarray
    processed_image: Optional[np.ndarray] = None
    detections: List[Detection] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Initialize processed_image if not provided."""
        if self.processed_image is None:
            self.processed_image = self.original_image.copy()


@dataclass
class PipelineResult:
    """Final result from vision pipeline.
    
    Contains all detections and processing information after
    the entire pipeline has completed.
    
    Attributes:
        original_image: The original input image.
        detections: List of all detections.
        processing_time_ms: Total processing time in milliseconds.
        metadata: Processing metadata from all steps.
    """
    original_image: np.ndarray
    detections: List[Detection]
    processing_time_ms: float
    metadata: Dict[str, Any]

    @property
    def detection_count(self) -> int:
        """Number of detections."""
        return len(self.detections)

    def get_detections_by_type(self, object_type: str) -> List[Detection]:
        """Get detections filtered by type."""
        return [d for d in self.detections if d.object_type == object_type]

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "detections": [d.to_dict() for d in self.detections],
            "detection_count": self.detection_count,
            "processing_time_ms": self.processing_time_ms,
            "metadata": self.metadata,
        }


