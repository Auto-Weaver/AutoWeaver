"""AutoWeaver — A framework for industrial vision inspection systems."""

from .camera import CameraBase, CameraConfig, DahengCamera, MockCamera
from .comm import (
    CommBase,
    CommEngine,
    CommWorker,
    WebSocketProtocol,
    WSServerProtocol,
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
    RegionDetection,
    PipelineContext,
    PipelineResult,
    ProcessStep,
    MaskApplyStep,
    SaveStep,
    SegmentDetection,
    SharpnessCheckStep,
    VisionPipeline,
    YOLOSegStep,
    create_step,
    list_available_steps,
    register_step,
)
from .reactive import EventBus, EventHandler, StateMachine, Transition
from .sensor import Sensor
from .worker import (
    AsyncPool,
    AsyncPoolConfig,
    AsyncPoolRegistry,
    BTClock,
    TickContext,
    TreeHandle,
    Worker,
    WorkerState,
    next_request_id,
)
from .tasks import Task, TaskBase

__version__ = "0.13.0"

__all__ = [
    # Worker framework
    "AsyncPool",
    "AsyncPoolConfig",
    "AsyncPoolRegistry",
    "BTClock",
    "TickContext",
    "TreeHandle",
    "Worker",
    "WorkerState",
    "next_request_id",
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
    "CommBase",
    "CommWorker",
    "CommEngine",
    "WebSocketProtocol",
    "WSServerProtocol",
    # Pipeline
    "BoundingBox",
    "CaptureStep",
    "Detection",
    "RegionDetection",
    "PipelineContext",
    "PipelineResult",
    "ProcessStep",
    "MaskApplyStep",
    "SaveStep",
    "SegmentDetection",
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
