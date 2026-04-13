"""gRPC client for communicating with the Rust motion-runtime.

This module wraps the gRPC stubs generated from proto/motion.proto,
providing a clean interface for BT leaf nodes to send goals,
read feedback, and halt axes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MotionGoal:
    axis_id: int
    position: float
    velocity: float
    timeout: float = 0.0


@dataclass
class MotionFeedback:
    axis_id: int
    position: float
    state: str
    progress: float


@dataclass
class MotionResult:
    axis_id: int
    success: bool
    final_position: float
    error_message: str = ""


class RuntimeClient:
    """Stub for motion-runtime gRPC client.

    Will be implemented when proto files are compiled.
    """

    def __init__(self, address: str = "localhost:50051"):
        self.address = address
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def send_goal(self, goal: MotionGoal) -> None:
        raise NotImplementedError("gRPC client not yet implemented")

    async def get_feedback(self, axis_id: int) -> MotionFeedback:
        raise NotImplementedError("gRPC client not yet implemented")

    async def get_result(self, axis_id: int) -> MotionResult | None:
        raise NotImplementedError("gRPC client not yet implemented")

    async def halt(self, axis_id: int) -> None:
        raise NotImplementedError("gRPC client not yet implemented")

    async def set_digital_output(self, module_id: int, channel: int, value: bool) -> None:
        raise NotImplementedError("gRPC client not yet implemented")

    async def get_digital_input(self, module_id: int, channel: int) -> bool:
        raise NotImplementedError("gRPC client not yet implemented")
