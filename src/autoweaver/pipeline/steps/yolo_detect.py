"""YOLO detection step for vision pipeline."""

import logging
from typing import TYPE_CHECKING, List

import numpy as np

from ..types import BoundingBox, Detection, PipelineContext
from .base import ProcessStep

if TYPE_CHECKING:
    from .tiling import TileInfo

logger = logging.getLogger(__name__)


class YOLODetectStep(ProcessStep):
    """YOLO object detection step.
    
    Uses Ultralytics YOLO for object detection. Supports various
    YOLO versions (v8, v11, etc.) and model sizes.
    
    Supports two modes:
    1. Direct mode: Run detection on ctx.processed_image
    2. Tile mode: Run detection on tiles from TileStep (if tiles exist in metadata)
    
    Parameters:
        model: Path to model file (.pt or .onnx).
        conf: Confidence threshold (0-1).
        iou: IoU threshold for NMS.
        half: Use FP16 inference for faster speed.
        batch_size: Batch size for tile inference (only used in tile mode).
        gpu_id: GPU device index (default 0).
    
    Note:
        GPU is required. Will raise RuntimeError if CUDA is not available.
    """

    def __init__(self, params: dict = None):
        super().__init__(params)
        
        self.model_path = self.params.get("model", "models/best.pt")
        self.confidence = self.params.get("conf", 0.5)
        self.iou_threshold = self.params.get("iou", 0.45)
        self.half = self.params.get("half", False)
        self.batch_size = self.params.get("batch_size", 1)
        self.gpu_id = self.params.get("gpu_id", 0)
        
        # Lazy load model
        self._model = None
        self._gpu_verified = False

    @property
    def name(self) -> str:
        return "yolo_detect"

    def _ensure_gpu(self) -> None:
        """Verify GPU is available. Raises RuntimeError if not."""
        if self._gpu_verified:
            return
        
        import torch
        
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA is not available. GPU is required for YOLO inference. "
                "Please check your CUDA installation and GPU drivers."
            )
        
        if self.gpu_id >= torch.cuda.device_count():
            raise RuntimeError(
                f"GPU {self.gpu_id} not found. Available GPUs: {torch.cuda.device_count()}"
            )
        
        logger.info(f"Using GPU {self.gpu_id}: {torch.cuda.get_device_name(self.gpu_id)}")
        self._gpu_verified = True

    @property
    def model(self):
        """Lazy-load the YOLO model."""
        if self._model is None:
            self._ensure_gpu()
            
            from ultralytics import YOLO
            
            logger.info(f"Loading YOLO model: {self.model_path}")
            self._model = YOLO(self.model_path)
            logger.info("YOLO model loaded on GPU")
        
        return self._model

    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Run YOLO detection on the image or tiles."""
        # Check if we're in tile mode
        tiles = ctx.metadata.get("tiles")
        
        if tiles:
            return self._process_tiles(ctx, tiles)
        else:
            return self._process_single(ctx)

    def _process_tiles(
        self, ctx: PipelineContext, tiles: List["TileInfo"]
    ) -> PipelineContext:
        """Run detection on multiple tiles."""
        total_detections = 0
        
        # Process tiles in batches
        for batch_start in range(0, len(tiles), self.batch_size):
            batch_end = min(batch_start + self.batch_size, len(tiles))
            batch_tiles = tiles[batch_start:batch_end]
            
            # Prepare batch images
            batch_images = [tile.image for tile in batch_tiles]
            
            # Run batch inference on GPU
            results = self.model.predict(
                batch_images,
                conf=self.confidence,
                iou=self.iou_threshold,
                half=self.half,
                device=self.gpu_id,
                verbose=False,
            )
            
            # Parse results for each tile
            for tile, result in zip(batch_tiles, results):
                tile_detections = self._parse_result(result, tile.width, tile.height)
                tile.detections = tile_detections
                total_detections += len(tile_detections)
        
        ctx.metadata["yolo_detection_count"] = total_detections
        ctx.metadata["tiles_processed"] = len(tiles)

        return ctx

    def _process_single(self, ctx: PipelineContext) -> PipelineContext:
        """Run detection on a single image (original behavior)."""
        # Run inference on GPU
        results = self.model.predict(
            ctx.processed_image,
            conf=self.confidence,
            iou=self.iou_threshold,
            half=self.half,
            device=self.gpu_id,
            verbose=False,
        )
        
        # Get scale and padding info for coordinate transformation
        scale = ctx.metadata.get("resize_scale", 1.0)
        padding = ctx.metadata.get("resize_padding", (0, 0, 0, 0))
        original_shape = ctx.metadata.get(
            "original_shape",
            ctx.original_image.shape[:2]
        )
        
        # Parse results
        for result in results:
            boxes = result.boxes
            
            for i in range(len(boxes)):
                box = boxes[i]
                
                # Get coordinates in resized image space
                xyxy = box.xyxy[0].cpu().numpy()
                cls_idx = int(box.cls[0].cpu().numpy())
                conf = float(box.conf[0].cpu().numpy())
                
                # Transform coordinates back to original image space
                x1, y1, x2, y2 = self._transform_coordinates(
                    xyxy, scale, padding, original_shape
                )
                
                # Get class name from model
                class_name = self.model.names.get(cls_idx, str(cls_idx))
                
                detection = Detection(
                    bbox=BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2),
                    object_type=class_name,
                    confidence=conf,
                )
                
                ctx.detections.append(detection)
        
        ctx.metadata["yolo_detection_count"] = len(ctx.detections)
        
        logger.debug(f"YOLO detected {len(ctx.detections)} objects")
        return ctx

    def _parse_result(
        self, result, tile_width: int, tile_height: int
    ) -> List[Detection]:
        """Parse YOLO result into Detection objects."""
        detections = []
        boxes = result.boxes
        
        for i in range(len(boxes)):
            box = boxes[i]
            
            xyxy = box.xyxy[0].cpu().numpy()
            cls_idx = int(box.cls[0].cpu().numpy())
            conf = float(box.conf[0].cpu().numpy())
            
            x1, y1, x2, y2 = xyxy
            
            # Clip to actual tile bounds (not padded area)
            x1 = max(0, min(float(x1), tile_width))
            y1 = max(0, min(float(y1), tile_height))
            x2 = max(0, min(float(x2), tile_width))
            y2 = max(0, min(float(y2), tile_height))
            
            # Skip invalid boxes
            if x2 <= x1 or y2 <= y1:
                continue
            
            # Get class name from model
            class_name = self.model.names.get(cls_idx, str(cls_idx))
            
            detections.append(Detection(
                bbox=BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2),
                object_type=class_name,
                confidence=conf,
            ))
        
        return detections

    def _transform_coordinates(
        self,
        xyxy,
        scale,
        padding,
        original_shape,
    ):
        """Transform coordinates from resized to original image space.
        
        Args:
            xyxy: [x1, y1, x2, y2] in resized image.
            scale: Resize scale factor.
            padding: (top, bottom, left, right) padding.
            original_shape: (height, width) of original image.
            
        Returns:
            Transformed (x1, y1, x2, y2) in original image space.
        """
        x1, y1, x2, y2 = xyxy
        
        # If we have padding info, remove it
        if isinstance(padding, tuple) and len(padding) == 4:
            top, bottom, left, right = padding
            x1 = x1 - left
            y1 = y1 - top
            x2 = x2 - left
            y2 = y2 - top
        
        # Scale back to original size
        if isinstance(scale, (int, float)):
            x1 = x1 / scale
            y1 = y1 / scale
            x2 = x2 / scale
            y2 = y2 / scale
        elif isinstance(scale, tuple):
            x1 = x1 / scale[0]
            y1 = y1 / scale[1]
            x2 = x2 / scale[0]
            y2 = y2 / scale[1]
        
        # Clip to image bounds
        h, w = original_shape
        x1 = max(0, min(x1, w))
        y1 = max(0, min(y1, h))
        x2 = max(0, min(x2, w))
        y2 = max(0, min(y2, h))
        
        return float(x1), float(y1), float(x2), float(y2)

