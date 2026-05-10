"""Comm protocol abstraction.

``CommBase`` defines the contract for a single communication endpoint:
how to receive a message, how to send one, how to close. Concrete
implementations sit one level down (``ModbusProtocol``,
``WebSocketProtocol``, ``WSServerProtocol``, ...) — they speak a
specific wire protocol but know nothing about which device or which
business meaning the messages carry.

Two-layer reading guide:

    Layer 1: ``CommBase``                 — protocol contract (this file)
    Layer 2: ``ModbusProtocol`` / ...     — concrete protocol mechanics
    Layer 3: ``CommSubsystem``            — Subsystem template that
                                            adopts a protocol and
                                            integrates it into BTClock
    Layer 4: application code             — assigns a protocol to a
                                            specific peer ("Nova5Link",
                                            "PlcLink") and gives the
                                            messages business meaning
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any


class CommBase(ABC):
    """Base contract for a comm protocol endpoint."""

    @abstractmethod
    def receive(self) -> Optional[Dict[str, Any]]:
        """Receive a message (non-blocking). Return None if nothing pending."""

    @abstractmethod
    def send(self, message: Dict[str, Any]) -> None:
        """Send a message."""

    @abstractmethod
    def close(self) -> None:
        """Close resources."""


__all__ = ["CommBase"]
