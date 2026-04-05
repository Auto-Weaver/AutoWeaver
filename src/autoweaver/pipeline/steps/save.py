"""Save step for persisting intermediate pipeline images to disk."""

import logging
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from ..types import PipelineContext
from .base import ProcessStep

logger = logging.getLogger(__name__)


class SaveStep(ProcessStep):
    """Save the current processed image to disk.

    A passthrough step that writes ``ctx.processed_image`` to a file
    without modifying the context. Can be inserted at any point in
    the pipeline to capture intermediate results.

    The filename is auto-generated from a timestamp by default, or
    can use a metadata key as the name.

    Parameters:
        output_dir: Directory to save images into (created if missing).
        format: Image file extension (default "png").
        name_key: Optional metadata key to use as filename.
            If the key exists in ``ctx.metadata``, its value becomes
            the filename stem. Otherwise falls back to timestamp.
        prefix: Optional prefix prepended to the filename.
        source: Which image to save (default "processed").
            "processed" saves ``ctx.processed_image``,
            "original" saves ``ctx.original_image``.
    """

    def __init__(self, params: dict = None):
        super().__init__(params)

        self.output_dir = Path(self.params.get("output_dir", "./saved"))
        self.format: str = self.params.get("format", "png")
        self.name_key: Optional[str] = self.params.get("name_key")
        self.prefix: str = self.params.get("prefix", "")
        self.source: str = self.params.get("source", "processed")

        self._counter = 0

    @property
    def name(self) -> str:
        return self._custom_name or "save"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Save the image to disk, pass context through unchanged."""
        image = self._get_image(ctx)
        if image is None:
            logger.warning("SaveStep: no image to save (source=%s)", self.source)
            return ctx

        self.output_dir.mkdir(parents=True, exist_ok=True)

        filename = self._build_filename(ctx)
        filepath = self.output_dir / filename

        cv2.imwrite(str(filepath), image)

        # Record what we saved in metadata
        saves = ctx.metadata.setdefault("saved_images", [])
        saves.append(str(filepath))

        logger.debug("Saved image to %s", filepath)
        return ctx

    def _get_image(self, ctx: PipelineContext) -> Optional[np.ndarray]:
        if self.source == "original":
            return ctx.original_image
        return ctx.processed_image

    def _build_filename(self, ctx: PipelineContext) -> str:
        stem = None

        if self.name_key and self.name_key in ctx.metadata:
            stem = str(ctx.metadata[self.name_key])

        if stem is None:
            stamp = int(time.time() * 1000)
            stem = f"{stamp}_{self._counter:04d}"
            self._counter += 1

        if self.prefix:
            stem = f"{self.prefix}_{stem}"

        return f"{stem}.{self.format}"
