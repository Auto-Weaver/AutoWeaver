"""YOLO instance segmentation step for vision pipeline."""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from ..types import BoundingBox, PipelineContext
from .base import ProcessStep

logger = logging.getLogger(__name__)


@dataclass
class SegmentResult:
    """Single instance segmentation result.

    Attributes:
        mask: Binary mask (H, W) in original image coordinates. uint8, 0 or 255.
        bbox: Bounding box of the mask region.
        confidence: Detection confidence score (0-1).
        class_id: Numeric class index from model.
        class_name: Class name from model labels.
    """

    mask: np.ndarray
    bbox: BoundingBox
    confidence: float
    class_id: int
    class_name: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (mask excluded for serialization)."""
        return {
            "bbox": self.bbox.to_dict(),
            "confidence": self.confidence,
            "class_id": self.class_id,
            "class_name": self.class_name,
            "mask_shape": list(self.mask.shape),
            "mask_area": int(np.count_nonzero(self.mask)),
        }


class YOLOSegStep(ProcessStep):
    """YOLO instance segmentation step.

    Uses Ultralytics YOLO in segment mode to produce per-instance
    binary masks. Output goes to ``ctx.metadata["segments"]`` as a
    list of :class:`SegmentResult`.

    Does **not** modify ``ctx.detections`` — segmentation results
    live in their own channel so they don't interfere with detection
    steps.

    Parameters:
        model: Path to YOLO seg model file (.pt or .onnx).
        conf: Confidence threshold (0-1, default 0.5).
        iou: IoU threshold for NMS (default 0.45).
        imgsz: Inference image size (default 1024).
        half: Use FP16 inference (default False).
        gpu_id: GPU device index (default 0).
        classes: Optional list of class indices to keep.

    Note:
        GPU is required. Will raise RuntimeError if CUDA is not available.
    """

    def __init__(self, params: dict = None):
        super().__init__(params)

        self.model_path: str = self.params.get("model", "models/best.pt")
        self.confidence: float = self.params.get("conf", 0.5)
        self.iou_threshold: float = self.params.get("iou", 0.45)
        self.imgsz: int = self.params.get("imgsz", 1024)
        self.half: bool = self.params.get("half", False)
        self.gpu_id: int = self.params.get("gpu_id", 0)
        self.classes: Optional[List[int]] = self.params.get("classes")

        self._model = None
        self._gpu_verified = False

    @property
    def name(self) -> str:
        return self._custom_name or "yolo_seg"

    def _ensure_gpu(self) -> None:
        """Verify GPU is available."""
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
                f"GPU {self.gpu_id} not found. "
                f"Available GPUs: {torch.cuda.device_count()}"
            )

        logger.info(
            "Using GPU %d: %s", self.gpu_id, torch.cuda.get_device_name(self.gpu_id)
        )
        self._gpu_verified = True

    @property
    def model(self):
        """Lazy-load the YOLO model."""
        if self._model is None:
            self._ensure_gpu()

            from ultralytics import YOLO

            logger.info("Loading YOLO seg model: %s", self.model_path)
            self._model = YOLO(self.model_path)
            logger.info("YOLO seg model loaded on GPU")

        return self._model

    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Run YOLO instance segmentation on the processed image."""
        image = ctx.processed_image
        if image is None:
            raise ValueError("YOLOSegStep requires ctx.processed_image")

        img_h, img_w = image.shape[:2]

        results = self.model.predict(
            image,
            conf=self.confidence,
            iou=self.iou_threshold,
            imgsz=self.imgsz,
            half=self.half,
            device=self.gpu_id,
            classes=self.classes,
            verbose=False,
        )

        segments: List[SegmentResult] = []

        for result in results:
            if result.masks is None:
                continue

            boxes = result.boxes
            masks = result.masks

            for i in range(len(boxes)):
                conf = float(boxes[i].conf[0].cpu().numpy())
                cls_id = int(boxes[i].cls[0].cpu().numpy())
                cls_name = self.model.names.get(cls_id, str(cls_id))

                # masks.data is (N, mask_h, mask_w) on model resolution.
                # Resize to original image size.
                mask_tensor = masks.data[i].cpu().numpy()
                mask_resized = self._resize_mask(mask_tensor, img_w, img_h)

                # Bounding box from mask
                bbox = self._mask_to_bbox(mask_resized)
                if bbox is None:
                    continue

                segments.append(
                    SegmentResult(
                        mask=mask_resized,
                        bbox=bbox,
                        confidence=conf,
                        class_id=cls_id,
                        class_name=cls_name,
                    )
                )

        # Sort by confidence descending
        segments.sort(key=lambda s: s.confidence, reverse=True)

        ctx.metadata["segments"] = segments
        ctx.metadata["segment_count"] = len(segments)

        logger.debug("YOLO seg produced %d segments", len(segments))
        return ctx

    @staticmethod
    def _resize_mask(mask: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
        """Resize a float mask to target size and binarize to uint8 0/255."""
        import cv2

        resized = cv2.resize(
            mask.astype(np.float32),
            (target_w, target_h),
            interpolation=cv2.INTER_LINEAR,
        )
        binary = (resized > 0.5).astype(np.uint8) * 255
        return binary

    @staticmethod
    def _mask_to_bbox(mask: np.ndarray) -> Optional[BoundingBox]:
        """Compute tight bounding box from a binary mask."""
        ys, xs = np.where(mask > 0)
        if len(ys) == 0:
            return None
        return BoundingBox(
            x1=float(xs.min()),
            y1=float(ys.min()),
            x2=float(xs.max()),
            y2=float(ys.max()),
        )
