"""Task abstractions for workflow systems."""

from .base import TaskBase
from .conditions import AlwaysFalseCondition, DoneCondition
from .protocol import SideTask, Task

__all__ = [
    "TaskBase",
    "Task",
    "SideTask",
    "DoneCondition",
    "AlwaysFalseCondition",
]
