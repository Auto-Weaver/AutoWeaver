"""Tiling steps for vision pipeline - slice and merge for large image inference."""

import logging
from dataclasses import dataclass
from typing import List

import numpy as np

from ..types import BoundingBox, Detection, PipelineContext
from .base import ProcessStep

logger = logging.getLogger(__name__)


@dataclass
class TileInfo:
    """Information about a single tile.
    
    Attributes:
        image: The tile image data.
        x_offset: X offset of tile in original image.
        y_offset: Y offset of tile in original image.
        width: Tile width.
        height: Tile height.
        detections: Detections found in this tile (populated by detection step).
    """
    image: np.ndarray
    x_offset: int
    y_offset: int
    width: int
    height: int
    detections: List[Detection] = None
    
    def __post_init__(self):
        if self.detections is None:
            self.detections = []


class TileStep(ProcessStep):
    """Slice image into overlapping tiles for detection.
    
    This step cuts a large image into smaller tiles that can be
    processed by YOLO. Overlapping tiles help detect objects
    that span tile boundaries.
    
    Parameters:
        tile_size: Size of each tile [width, height] or single int for square.
        overlap: Overlap ratio between adjacent tiles (0-1).
    """

    def __init__(self, params: dict = None):
        super().__init__(params)
        size = self.params.get("tile_size", 640)
        if isinstance(size, int):
            self.tile_size = (size, size)
        else:
            self.tile_size = tuple(size)
        self.overlap = self.params.get("overlap", 0.2)

    @property
    def name(self) -> str:
        return "tile"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Slice the image into tiles."""
        image = ctx.original_image
        h, w = image.shape[:2]
        tile_w, tile_h = self.tile_size
        
        # Calculate stride based on overlap
        stride_x = max(1, int(tile_w * (1 - self.overlap)))
        stride_y = max(1, int(tile_h * (1 - self.overlap)))
        
        # Generate tile positions
        x_positions = self._get_positions(w, tile_w, stride_x)
        y_positions = self._get_positions(h, tile_h, stride_y)
        
        tiles: List[TileInfo] = []
        
        for y0 in y_positions:
            for x0 in x_positions:
                tile_img = image[y0:y0 + tile_h, x0:x0 + tile_w]
                
                tiles.append(TileInfo(
                    image=tile_img,
                    x_offset=x0,
                    y_offset=y0,
                    width=tile_w,
                    height=tile_h,
                ))
        
        # Store tiles in metadata for detection step
        ctx.metadata["tiles"] = tiles
        ctx.metadata["tile_size"] = self.tile_size
        ctx.metadata["original_size"] = (w, h)
        ctx.metadata["tile_count"] = len(tiles)

        return ctx

    def _get_positions(self, length: int, tile_size: int, stride: int) -> List[int]:
        """Calculate tile start positions along one axis."""
        positions = [0]
        pos = 0
        
        while pos + tile_size < length:
            pos += stride
            # Ensure last tile reaches the edge
            if pos + tile_size >= length:
                positions.append(length - tile_size)
                break
            positions.append(pos)
        
        return sorted(set(positions))


class MergeTilesStep(ProcessStep):
    """Merge tile detections back to original image coordinates.
    
    This step takes detections from all tiles and:
    1. Transforms coordinates back to original image space
    2. Applies NMS to remove duplicate detections from overlapping tiles
    
    Parameters:
        iou_threshold: IoU threshold for merging overlapping detections.
    """

    def __init__(self, params: dict = None):
        super().__init__(params)
        self.iou_threshold = self.params.get("iou_threshold", 0.5)

    @property
    def name(self) -> str:
        return "merge_tiles"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Merge tile detections to original image space."""
        tiles: List[TileInfo] = ctx.metadata.get("tiles", [])
        original_size = ctx.metadata.get("original_size", ctx.original_image.shape[:2][::-1])
        
        if not tiles:
            logger.warning("No tiles found in context, skipping merge")
            return ctx
        
        # Collect all detections with transformed coordinates
        all_detections: List[Detection] = []
        
        for tile in tiles:
            for det in tile.detections:
                # Transform bbox to original image coordinates
                transformed_bbox = BoundingBox(
                    x1=det.bbox.x1 + tile.x_offset,
                    y1=det.bbox.y1 + tile.y_offset,
                    x2=min(det.bbox.x2 + tile.x_offset, original_size[0]),
                    y2=min(det.bbox.y2 + tile.y_offset, original_size[1]),
                )
                
                # Clip to image bounds
                transformed_bbox = BoundingBox(
                    x1=max(0, transformed_bbox.x1),
                    y1=max(0, transformed_bbox.y1),
                    x2=min(original_size[0], transformed_bbox.x2),
                    y2=min(original_size[1], transformed_bbox.y2),
                )
                
                # Skip invalid boxes
                if transformed_bbox.width <= 0 or transformed_bbox.height <= 0:
                    continue
                
                all_detections.append(Detection(
                    bbox=transformed_bbox,
                    object_type=det.object_type,
                    confidence=det.confidence,
                    detection_id=det.detection_id,
                ))

        # Apply NMS to remove duplicates from overlapping regions
        merged = self._apply_nms(all_detections)

        ctx.detections = merged
        ctx.metadata["pre_merge_count"] = len(all_detections)
        ctx.metadata["post_merge_count"] = len(merged)
        ctx.metadata["merge_removed"] = len(all_detections) - len(merged)
        
        # Clean up tiles from metadata to save memory
        del ctx.metadata["tiles"]
        
        return ctx

    def _apply_nms(self, detections: List[Detection]) -> List[Detection]:
        """Apply Non-Maximum Suppression to merged detections."""
        if not detections:
            return []
        
        # Group by class for per-class NMS
        by_class = {}
        for det in detections:
            key = det.object_type
            if key not in by_class:
                by_class[key] = []
            by_class[key].append(det)
        
        result = []
        for class_dets in by_class.values():
            result.extend(self._nms_single_class(class_dets))
        
        return result

    def _nms_single_class(self, detections: List[Detection]) -> List[Detection]:
        """Apply NMS to detections of a single class."""
        if not detections:
            return []
        
        # Sort by confidence descending
        detections = sorted(detections, key=lambda d: d.confidence, reverse=True)
        
        keep = []
        while detections:
            best = detections.pop(0)
            keep.append(best)
            
            # Remove detections with high IoU
            detections = [
                d for d in detections
                if self._iou(best, d) < self.iou_threshold
            ]
        
        return keep

    def _iou(self, det1: Detection, det2: Detection) -> float:
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

