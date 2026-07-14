"""Postprocessing steps for vision pipeline."""

import logging
from typing import List, Protocol, runtime_checkable

from ..types import BoundingBox, PipelineContext
from .base import ProcessStep

logger = logging.getLogger(__name__)


@runtime_checkable
class BoxLike(Protocol):
    """Structural contract these postprocess steps require of a payload.

    NMS / Filter / Sort only ever touch a box, a class label, and a
    confidence — so that is all they demand, declared right where they are
    consumed. Any object exposing these three attributes qualifies
    (``isinstance(x, BoxLike)`` works because this is ``runtime_checkable``);
    :class:`~autoweaver.pipeline.types.Detection` and
    :class:`~autoweaver.pipeline.types.RegionDetection` both satisfy it, but
    nothing forces a pipeline payload to. There is deliberately no
    framework-wide "every detection must have field X" floor.

    Attributes:
        bbox: A bounding box exposing ``.area`` and ``.center``.
        object_type: Class label used for per-class grouping / filtering.
        confidence: Score used for thresholding and sorting.
    """

    bbox: BoundingBox
    object_type: str
    confidence: float


class NMSStep(ProcessStep[BoxLike]):
    """Apply Non-Maximum Suppression to filter detections.
    
    This step removes overlapping detections, keeping only the
    ones with highest confidence.
    
    Parameters:
        iou_threshold: IoU threshold for suppression.
        score_threshold: Minimum confidence to keep.
        class_agnostic: If True, apply NMS across all classes.
    """

    def __init__(self, params: dict = None):
        super().__init__(params)
        self.iou_threshold = self.params.get("iou_threshold", 0.4)
        self.score_threshold = self.params.get("score_threshold", 0.0)
        self.class_agnostic = self.params.get("class_agnostic", False)

    @property
    def name(self) -> str:
        return "nms"

    def process(self, ctx: PipelineContext[BoxLike]) -> PipelineContext[BoxLike]:
        """Apply NMS to detections."""
        if not ctx.detections:
            return ctx
        
        original_count = len(ctx.detections)
        
        if self.class_agnostic:
            ctx.detections = self._nms(ctx.detections)
        else:
            # Group by class and apply NMS per class
            by_class = {}
            for det in ctx.detections:
                key = det.object_type
                if key not in by_class:
                    by_class[key] = []
                by_class[key].append(det)
            
            filtered = []
            for dets in by_class.values():
                filtered.extend(self._nms(dets))
            
            ctx.detections = filtered
        
        removed = original_count - len(ctx.detections)
        ctx.metadata["nms_removed"] = removed
        
        logger.debug(
            f"NMS: {original_count} -> {len(ctx.detections)} "
            f"(removed {removed})"
        )
        
        return ctx

    def _nms(self, detections: List[BoxLike]) -> List[BoxLike]:
        """Apply NMS to a list of detections."""
        if not detections:
            return []
        
        # Filter by score threshold first
        detections = [
            d for d in detections
            if d.confidence >= self.score_threshold
        ]
        
        if not detections:
            return []
        
        # Sort by confidence (descending)
        detections = sorted(
            detections,
            key=lambda d: d.confidence,
            reverse=True
        )
        
        keep = []
        
        while detections:
            # Keep the highest scoring detection
            best = detections.pop(0)
            keep.append(best)
            
            # Remove detections with high IoU
            detections = [
                d for d in detections
                if self._iou(best, d) < self.iou_threshold
            ]
        
        return keep

    def _iou(self, det1: BoxLike, det2: BoxLike) -> float:
        """Calculate IoU between two detections."""
        box1 = det1.bbox
        box2 = det2.bbox
        
        # Intersection
        x1 = max(box1.x1, box2.x1)
        y1 = max(box1.y1, box2.y1)
        x2 = min(box1.x2, box2.x2)
        y2 = min(box1.y2, box2.y2)
        
        if x2 <= x1 or y2 <= y1:
            return 0.0
        
        intersection = (x2 - x1) * (y2 - y1)
        
        # Union
        area1 = box1.area
        area2 = box2.area
        union = area1 + area2 - intersection
        
        if union <= 0:
            return 0.0
        
        return intersection / union


class FilterStep(ProcessStep[BoxLike]):
    """Filter detections by various criteria.
    
    Parameters:
        min_confidence: Minimum confidence threshold.
        max_confidence: Maximum confidence threshold.
        min_area: Minimum bounding box area in pixels.
        max_area: Maximum bounding box area in pixels.
        classes: List of object types to keep.
    """

    def __init__(self, params: dict = None):
        super().__init__(params)
        self.min_confidence = self.params.get("min_confidence", 0.0)
        self.max_confidence = self.params.get("max_confidence", 1.0)
        self.min_area = self.params.get("min_area", 0)
        self.max_area = self.params.get("max_area", float("inf"))
        self.classes = self.params.get("classes", None)

    @property
    def name(self) -> str:
        return "filter"

    def process(self, ctx: PipelineContext[BoxLike]) -> PipelineContext[BoxLike]:
        """Filter detections by criteria."""
        original_count = len(ctx.detections)
        
        filtered = []
        for det in ctx.detections:
            # Confidence filter
            if not (self.min_confidence <= det.confidence <= self.max_confidence):
                continue
            
            # Area filter
            area = det.bbox.area
            if not (self.min_area <= area <= self.max_area):
                continue
            
            # Class filter
            if self.classes is not None:
                if det.object_type not in self.classes:
                    continue
            
            filtered.append(det)
        
        ctx.detections = filtered
        removed = original_count - len(filtered)
        ctx.metadata["filter_removed"] = removed
        
        logger.debug(
            f"Filter: {original_count} -> {len(filtered)} "
            f"(removed {removed})"
        )
        
        return ctx


class SortStep(ProcessStep[BoxLike]):
    """Sort detections by specified criteria.
    
    Parameters:
        by: Sort key - 'confidence', 'area', 'x', 'y'.
        ascending: Sort in ascending order.
    """

    def __init__(self, params: dict = None):
        super().__init__(params)
        self.by = self.params.get("by", "confidence")
        self.ascending = self.params.get("ascending", False)

    @property
    def name(self) -> str:
        return "sort"

    def process(self, ctx: PipelineContext[BoxLike]) -> PipelineContext[BoxLike]:
        """Sort detections."""
        if not ctx.detections:
            return ctx
        
        key_funcs = {
            "confidence": lambda d: d.confidence,
            "area": lambda d: d.bbox.area,
            "x": lambda d: d.bbox.center[0],
            "y": lambda d: d.bbox.center[1],
        }
        
        key_func = key_funcs.get(self.by, key_funcs["confidence"])
        
        ctx.detections = sorted(
            ctx.detections,
            key=key_func,
            reverse=not self.ascending
        )
        
        logger.debug(f"Sorted detections by {self.by}")
        return ctx


