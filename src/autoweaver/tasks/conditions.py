"""Completion conditions for tasks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DoneCondition(ABC):
    """Interface for determining task completion."""

    @abstractmethod
    def check(self, stats: Any, result: Any) -> bool:
        """Return True if task should stop."""
        raise NotImplementedError

    @abstractmethod
    def reset(self) -> None:
        """Reset internal state."""
        raise NotImplementedError


class AlwaysFalseCondition(DoneCondition):
    """Never finishes; keeps existing infinite-loop behavior."""

    def check(self, stats: Any, result: Any) -> bool:
        return False

    def reset(self) -> None:
        return None
