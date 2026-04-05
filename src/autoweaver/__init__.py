"""AutoWeaver — A framework for industrial vision inspection systems."""

from .camera import CameraBase, CameraConfig, DahengCamera, MockCamera
from .comm import CommSignalBase, CommSideTask, ModbusAdapter, WebSocketAdapter, WebSocketServerAdapter
from .pipeline import (
    BoundingBox,
    CaptureStep,
    Detection,
    PipelineContext,
    PipelineResult,
    ProcessStep,
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
from .tasks import (
    Adjuster,
    AlwaysFalseCondition,
    DoneCondition,
    ExposureAdjuster,
    RetryCaptureTask,
    SideTask,
    Task,
    TaskBase,
)
from .workflow import WorkflowEngine, WorkflowDefinition, load_workflow_from_yaml

__version__ = "0.4.0"

__all__ = [
    "CameraBase",
    "CameraConfig",
    "CaptureStep",
    "CommSignalBase",
    "CommSideTask",
    "MockCamera",
    "ModbusAdapter",
    "WebSocketAdapter",
    "WebSocketServerAdapter",
    "DahengCamera",
    "Detection",
    "BoundingBox",
    "PipelineContext",
    "PipelineResult",
    "VisionPipeline",
    "ProcessStep",
    "SharpnessCheckStep",
    "YOLOSegStep",
    "SegmentResult",
    "SaveStep",
    "create_step",
    "register_step",
    "list_available_steps",
    "EventBus",
    "EventHandler",
    "StateMachine",
    "Transition",
    "TaskBase",
    "Task",
    "SideTask",
    "DoneCondition",
    "AlwaysFalseCondition",
    "Adjuster",
    "ExposureAdjuster",
    "RetryCaptureTask",
    "WorkflowEngine",
    "WorkflowDefinition",
    "load_workflow_from_yaml",
]
