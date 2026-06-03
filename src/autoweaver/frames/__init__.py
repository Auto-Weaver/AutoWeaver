"""Coordinate-frame resolution for multi-arm cells.

Static calibration edges come from a YAML file; dynamic edges (flange pose,
compensation) are bound in code and read from a per-tick WorldBoard snapshot.
See docs/evo/008-frames.md for the design contract.
"""

from autoweaver.frames.frames import (
    Frames,
    FrameNotFound,
    FramesDisconnected,
    FramesError,
)
from autoweaver.frames.schema import CalibrationSchemaError

__all__ = [
    "Frames",
    "FramesError",
    "FrameNotFound",
    "FramesDisconnected",
    "CalibrationSchemaError",
]
