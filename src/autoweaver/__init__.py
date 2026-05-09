"""AutoWeaver — A framework for industrial vision inspection systems."""

from .camera import CameraBase, CameraConfig, DahengCamera, MockCamera
from .comm import (
    CommSignalBase,
    CommSubsystem,
    ModbusAdapter,
    WebSocketAdapter,
    WebSocketServerAdapter,
)
from .motion_policy.action import Action, ActionResult
from .motion_policy.blackboard import Blackboard
from .motion_policy.nodes.leaf.notify import NotifyLeaf
from .motion_policy.nodes.leaf.wait_for import WaitFor
from .motion_policy.world_board import Snapshot, WorldBoard
from .pipeline import (
    BoundingBox,
    CaptureStep,
    Detection,
    PipelineContext,
    PipelineResult,
    ProcessStep,
    MaskApplyStep,
    SaveStep,
    SegmentResult,
    SharpnessCheckStep,
    VisionPipeline,
    YOLOSegStep,
    create_step,
    list_available_steps,
    register_step,
)
from .reactive import EventBus, EventHandler, StateMachine, Transition
from .sensor import Sensor
from .subsystem import (
    AsyncPool,
    AsyncPoolConfig,
    AsyncPoolRegistry,
    BTClock,
    Subsystem,
    SubsystemState,
    TickContext,
    TreeHandle,
)
from .tasks import Task, TaskBase

__version__ = "0.5.1"

__all__ = [
    # Subsystem framework
    "AsyncPool",
    "AsyncPoolConfig",
    "AsyncPoolRegistry",
    "BTClock",
    "Subsystem",
    "SubsystemState",
    "TickContext",
    "TreeHandle",
    # BT engine
    "Action",
    "ActionResult",
    "Blackboard",
    "NotifyLeaf",
    "Snapshot",
    "WaitFor",
    "WorldBoard",
    # Sensor
    "Sensor",
    # Camera
    "CameraBase",
    "CameraConfig",
    "DahengCamera",
    "MockCamera",
    # Comm
    "CommSignalBase",
    "CommSubsystem",
    "ModbusAdapter",
    "WebSocketAdapter",
    "WebSocketServerAdapter",
    # Pipeline
    "BoundingBox",
    "CaptureStep",
    "Detection",
    "PipelineContext",
    "PipelineResult",
    "ProcessStep",
    "MaskApplyStep",
    "SaveStep",
    "SegmentResult",
    "SharpnessCheckStep",
    "VisionPipeline",
    "YOLOSegStep",
    "create_step",
    "list_available_steps",
    "register_step",
    # Reactive
    "EventBus",
    "EventHandler",
    "StateMachine",
    "Transition",
    # Tasks
    "Task",
    "TaskBase",
]
