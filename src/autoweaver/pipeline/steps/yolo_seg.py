"""YOLO instance segmentation step for vision pipeline."""

import logging
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from ..types import BoundingBox, PipelineContext, RegionDetection
from .base import ProcessStep

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class SegmentDetection(RegionDetection):
    """A :class:`RegionDetection` produced by :class:`YOLOSegStep`.

    Adds the numeric class index on top of the generic region payload.
    The model's class name is carried in the inherited ``object_type``
    field; the mask is bbox-local (see :class:`RegionDetection`).

    Attributes:
        class_id: Numeric class index from the model.
    """

    class_id: int


class YOLOSegStep(ProcessStep[SegmentDetection]):
    """YOLO instance segmentation step.

    Uses Ultralytics YOLO in segment mode to produce per-instance
    binary masks. Each instance is appended to ``ctx.detections`` as a
    :class:`SegmentDetection` (a :class:`RegionDetection` carrying a
    bbox-local mask); ``ctx.metadata["segment_count"]`` records how many
    this step produced.

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

    def process(self, ctx: PipelineContext[SegmentDetection]) -> PipelineContext[SegmentDetection]:
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

        segments: List[SegmentDetection] = []

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

                # Bounding box from mask (full-frame coordinates)
                bbox = self._mask_to_bbox(mask_resized)
                if bbox is None:
                    continue

                # Crop the mask to bbox-local coordinates — a per-detection
                # full-frame mask would be ~12 MB on a 4000x3000 image.
                mask_local = self._crop_mask_to_bbox(mask_resized, bbox)

                segments.append(
                    SegmentDetection(
                        bbox=bbox,
                        object_type=cls_name,
                        confidence=conf,
                        mask=mask_local,
                        area_px=int(np.count_nonzero(mask_local)),
                        class_id=cls_id,
                    )
                )

        # Sort by confidence descending
        segments.sort(key=lambda s: s.confidence, reverse=True)

        ctx.detections.extend(segments)
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

    @staticmethod
    def _crop_mask_to_bbox(mask: np.ndarray, bbox: BoundingBox) -> np.ndarray:
        """Crop a full-frame mask down to its bbox-local window.

        ``_mask_to_bbox`` returns inclusive max coordinates, so the local
        window is ``[y1..y2], [x1..x2]`` inclusive. Paste ``mask_local`` back
        at ``(bbox.x1, bbox.y1)`` to reconstruct the full-frame mask.
        """
        x1, y1 = int(bbox.x1), int(bbox.y1)
        x2, y2 = int(bbox.x2), int(bbox.y2)
        return np.ascontiguousarray(mask[y1 : y2 + 1, x1 : x2 + 1])
