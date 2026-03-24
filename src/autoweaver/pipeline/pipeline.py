"""Vision processing pipeline."""

import logging
import time
from typing import List

from .types import PipelineContext, PipelineResult

logger = logging.getLogger(__name__)


class VisionPipeline:
    """Vision processing pipeline.

    A pipeline consists of multiple processing steps that are
    executed in sequence. Each step receives a PipelineContext
    and returns an updated context.

    The first step is typically a CaptureStep that acquires an
    image from a camera. Subsequent steps process the image.

    Example:
        >>> pipeline = VisionPipeline()
        >>> pipeline.add_step(CaptureStep(camera, {"exposure_time": 5000.0}))
        >>> pipeline.add_step(YOLODetectStep({"model": "best.pt"}))
        >>> result = pipeline.run()
        >>> print(f"Found {result.detection_count} objects")
    """

    def __init__(self):
        """Initialize empty pipeline."""
        self._steps: List["ProcessStep"] = []

    def add_step(self, step: "ProcessStep") -> "VisionPipeline":
        """Add a processing step to the pipeline.

        Args:
            step: Processing step to add.

        Returns:
            Self for method chaining.
        """
        self._steps.append(step)
        logger.info(f"Added pipeline step: {step.name}")
        return self

    def run(self) -> PipelineResult:
        """Run the pipeline.

        Returns:
            PipelineResult with detections and metadata.
        """
        start_time = time.time()

        ctx = PipelineContext()

        for step in self._steps:
            step_start = time.time()

            try:
                ctx = step.process(ctx)
            except Exception as e:
                logger.error(f"Step '{step.name}' failed: {e}")
                raise

            step_time = (time.time() - step_start) * 1000
            ctx.metadata[f"{step.name}_time_ms"] = step_time

        total_time = (time.time() - start_time) * 1000

        return PipelineResult(
            detections=ctx.detections,
            processing_time_ms=total_time,
            metadata=ctx.metadata,
            original_image=ctx.original_image,
            processed_image=ctx.processed_image,
        )

    def clear(self) -> None:
        """Remove all steps from the pipeline."""
        self._steps.clear()

    @property
    def step_count(self) -> int:
        """Number of steps in the pipeline."""
        return len(self._steps)

    @property
    def step_names(self) -> List[str]:
        """Names of all steps in order."""
        return [step.name for step in self._steps]

    @classmethod
    def from_config(cls, config: dict) -> "VisionPipeline":
        """Build pipeline from configuration.
        
        Args:
            config: Pipeline configuration with 'steps' list.
            
        Returns:
            Configured VisionPipeline instance.
            
        Example config:
            {
                "pipeline": {
                    "steps": [
                        {"name": "resize", "type": "resize", "params": {"size": [640, 640]}},
                        {"name": "detect", "type": "yolo", "params": {"model": "best.pt"}},
                    ]
                }
            }
        """
        from .steps import create_step
        
        pipeline = cls()
        
        pipeline_config = config.get("pipeline", config)
        steps_config = pipeline_config.get("steps", [])
        
        for step_config in steps_config:
            step = create_step(step_config)
            pipeline.add_step(step)
        
        logger.info(f"Created pipeline with {len(steps_config)} steps")
        return pipeline


# Import ProcessStep at module level for type hints
from .steps.base import ProcessStep  # noqa: E402, F401
