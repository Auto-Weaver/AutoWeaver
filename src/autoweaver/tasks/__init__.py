"""Task abstractions — stateful components held inside Workers."""

from .base import TaskBase
from .protocol import Task

__all__ = ["TaskBase", "Task"]
