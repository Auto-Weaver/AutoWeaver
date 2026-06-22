"""Observability for AutoWeaver — passive board consumers, not control.

The kernel publishes arm pose / joints / status to the ``WorldBoard``
every tick. ``telemetry`` reads that stream and persists it for offline
analysis; it never writes control state and never sits on the motion
path. Keep it that way — recording is downstream of the kernel, not part
of it.
"""

from autoweaver.telemetry.trajectory import TrajectoryRecorder

__all__ = ["TrajectoryRecorder"]
