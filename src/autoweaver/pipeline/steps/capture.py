"""Camera capture step for vision pipeline."""

import logging
from typing import Any, Dict, Optional

from ...camera.base import CameraBase
from ..types import PipelineContext
from .base import ProcessStep

logger = logging.getLogger(__name__)


class CaptureStep(ProcessStep):
    """Capture an image from a camera.

    This step sets exposure/gain, captures a frame, and stores it
    in the pipeline context. Typically the first step in a pipeline.

    Args:
        camera: Camera instance (managed by the Task layer).
        params: Optional parameters.
            - exposure_time: Exposure time in microseconds.
            - gain: Gain value.

    Example:
        >>> step = CaptureStep(camera, {"exposure_time": 5000.0})
        >>> ctx = step.process(PipelineContext())
        >>> assert ctx.original_image is not None
    """

    def __init__(self, camera: CameraBase, params: Optional[Dict[str, Any]] = None):
        super().__init__(params)
        self._camera = camera

    def process(self, ctx: PipelineContext) -> PipelineContext:
        exposure_time = self.params.get("exposure_time")
        if exposure_time is not None:
            self._camera.set_exposure_time(exposure_time)

        gain = self.params.get("gain")
        if gain is not None:
            self._camera.set_gain(gain)

        image = self._camera.capture()

        ctx.original_image = image
        ctx.processed_image = image.copy()

        return ctx
