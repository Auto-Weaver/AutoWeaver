"""Closed-loop capture task: capture → evaluate → adjust → retry."""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Protocol, runtime_checkable

from ..camera.base import CameraBase
from ..pipeline.pipeline import VisionPipeline
from .base import TaskBase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adjuster protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Adjuster(Protocol):
    """Protocol for hardware/parameter adjusters.

    An adjuster modifies something (camera exposure, Z-axis position, etc.)
    to improve capture quality on the next attempt.

    Implementations:
        - ExposureAdjuster: adjusts camera exposure time
        - User-defined: Z-axis, focus motor, lighting, etc.
    """

    def adjust(self, metadata: dict, attempt: int) -> None:
        """Adjust hardware/parameters based on current quality metrics.

        Args:
            metadata: Pipeline result metadata (contains quality scores).
            attempt: Current attempt number (0-based).
        """
        ...


# ---------------------------------------------------------------------------
# Built-in adjusters
# ---------------------------------------------------------------------------

class ExposureAdjuster:
    """Adjust camera exposure time by a fixed delta per attempt.

    Args:
        camera: Camera instance.
        delta: Exposure time increment in microseconds per attempt.
            Positive = brighter, negative = darker.

    Example:
        >>> adjuster = ExposureAdjuster(camera, delta=2000.0)
        >>> adjuster.adjust(metadata, attempt=0)  # +2000us
    """

    def __init__(self, camera: CameraBase, delta: float = 1000.0):
        self._camera = camera
        self._delta = delta

    def adjust(self, metadata: dict, attempt: int) -> None:
        current = self._camera.config.exposure_time or 10000.0
        new_val = max(100.0, current + self._delta)
        self._camera.set_exposure_time(new_val)
        logger.info(f"ExposureAdjuster: {current:.0f} → {new_val:.0f} us")


# ---------------------------------------------------------------------------
# RetryCaptureTask
# ---------------------------------------------------------------------------

class RetryCaptureTask(TaskBase):
    """Closed-loop capture: run pipeline, check quality, adjust, retry.

    Engine calls tick() to trigger one capture cycle. Internally the task
    retries up to max_retries times. On success it broadcasts
    "capture_ok"; on failure it broadcasts "capture_failed".

    Args:
        pipeline: A VisionPipeline containing CaptureStep + quality check
            steps (e.g. SharpnessCheckStep).
        adjusters: List of adjusters to apply between retries.
        quality_key: Metadata key to read quality score from (default: "sharpness").
        threshold: Minimum acceptable quality score (default: 100.0).
        max_retries: Maximum number of capture attempts (default: 3).

    Example:
        >>> pipeline = VisionPipeline()
        >>> pipeline.add_step(CaptureStep(camera, {"exposure_time": 5000.0}))
        >>> pipeline.add_step(SharpnessCheckStep())
        >>>
        >>> task = RetryCaptureTask(
        ...     pipeline=pipeline,
        ...     adjusters=[ExposureAdjuster(camera, delta=2000.0)],
        ...     quality_key="sharpness",
        ...     threshold=100.0,
        ...     max_retries=3,
        ... )
    """

    name = "retry_capture"

    def __init__(
        self,
        pipeline: VisionPipeline,
        adjusters: Optional[List[Adjuster]] = None,
        quality_key: str = "sharpness",
        threshold: float = 100.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__()
        self._pipeline = pipeline
        self._adjusters = adjusters or []
        self._quality_key = quality_key
        self._threshold = threshold
        self._max_retries = max_retries

        # Last successful result (accessible after tick)
        self.last_result = None

    def tick(self, data: Any) -> None:
        """Execute one capture cycle with retries.

        Args:
            data: Trigger data from engine (not used directly).
        """
        self.last_result = None

        for attempt in range(self._max_retries):
            result = self._pipeline.run()
            score = result.metadata.get(self._quality_key, 0.0)

            logger.info(
                f"Capture attempt {attempt + 1}/{self._max_retries}: "
                f"{self._quality_key}={score:.1f} (threshold={self._threshold})"
            )

            if score >= self._threshold:
                self.last_result = result
                self.broadcast("capture_ok", {
                    "result": result,
                    "attempts": attempt + 1,
                    "score": score,
                })
                return

            # Apply all adjusters before next attempt
            if attempt < self._max_retries - 1:
                for adj in self._adjusters:
                    adj.adjust(result.metadata, attempt)

        # All attempts exhausted
        logger.warning(
            f"Capture failed after {self._max_retries} attempts "
            f"(best {self._quality_key}={score:.1f})"
        )
        self.last_result = result
        self.broadcast("capture_failed", {
            "result": result,
            "attempts": self._max_retries,
            "score": score,
        })
