"""Task abstractions for workflow systems."""

from ._base import TaskBase
from ._conditions import AlwaysFalseCondition, DoneCondition
from ._protocol import SideTask, Task

__all__ = [
    "TaskBase",
    "Task",
    "SideTask",
    "DoneCondition",
    "AlwaysFalseCondition",
]
