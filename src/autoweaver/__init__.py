"""AutoWeaver — A framework for industrial vision inspection systems."""

from .camera import CameraBase, CameraConfig, DahengCamera, MockCamera
from .comm import CommSignalBase, CommSideTask, ModbusAdapter, WebSocketAdapter
from .pipeline import (
    BoundingBox,
    Detection,
    PipelineContext,
    PipelineResult,
    ProcessStep,
    VisionPipeline,
    create_step,
    list_available_steps,
    register_step,
)
from .reactive import EventBus, EventHandler, StateMachine, Transition
from .tasks import (
    AlwaysFalseCondition,
    DoneCondition,
    SideTask,
    Task,
    TaskBase,
)
from .workflow import WorkflowEngine, WorkflowDefinition, load_workflow_from_yaml

__version__ = "0.4.0"

__all__ = [
    "CameraBase",
    "CameraConfig",
    "CommSignalBase",
    "CommSideTask",
    "MockCamera",
    "ModbusAdapter",
    "WebSocketAdapter",
    "DahengCamera",
    "Detection",
    "BoundingBox",
    "PipelineContext",
    "PipelineResult",
    "VisionPipeline",
    "ProcessStep",
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
    "WorkflowEngine",
    "WorkflowDefinition",
    "load_workflow_from_yaml",
]
