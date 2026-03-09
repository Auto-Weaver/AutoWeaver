"""Comm signal channel abstraction (protocol-agnostic)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any


class CommSignalBase(ABC):
    """Base class for comm signal I/O."""

    @abstractmethod
    def receive(self) -> Optional[Dict[str, Any]]:
        """Receive a comm message (non-blocking)."""

    @abstractmethod
    def send(self, message: Dict[str, Any]) -> None:
        """Send a comm response message."""

    @abstractmethod
    def close(self) -> None:
        """Close resources."""


__all__ = ["CommSignalBase"]
