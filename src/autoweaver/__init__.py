"""AutoWeaver — A framework for industrial vision inspection systems."""

from .sensor.camera import (
    CameraBase,
    CameraConfig,
    CameraObservation,
    DahengCamera,
    MockCamera,
)
from .comm import (
    CommBase,
    CommEngine,
    CommWorker,
    WebSocketProtocol,
    WSServerProtocol,
)
from .motion_policy.batch import (
    Batch,
    BatchInfo,
    BatchResult,
    BatchState,
    ExitReason,
    TeardownOutcome,
)
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
from .sensor import (
    Derivation,
    Observation,
    Observer,
    ObserverSpeed,
    PixelTransform,
    Sensor,
)
from .worker import (
    AsyncPool,
    AsyncPoolConfig,
    AsyncPoolRegistry,
    BatchHandle,
    BTClock,
    TickContext,
    Worker,
    WorkerState,
    next_request_id,
)
from .tasks import Task, TaskBase

__version__ = "0.18.0"

__all__ = [
    # Worker framework
    "AsyncPool",
    "AsyncPoolConfig",
    "AsyncPoolRegistry",
    "BatchHandle",
    "BTClock",
    "TickContext",
    "Worker",
    "WorkerState",
    "next_request_id",
    # BT engine
    "Batch",
    "BatchInfo",
    "BatchResult",
    "BatchState",
    "ExitReason",
    "TeardownOutcome",
    "Blackboard",
    "NotifyLeaf",
    "Snapshot",
    "WaitFor",
    "WorldBoard",
    # Sensor / Observation (EVO-011)
    "Derivation",
    "Observation",
    "Observer",
    "ObserverSpeed",
    "PixelTransform",
    "Sensor",
    # Camera
    "CameraBase",
    "CameraConfig",
    "CameraObservation",
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
