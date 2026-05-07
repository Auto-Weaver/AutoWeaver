from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autoweaver.motion_policy.blackboard import Blackboard
    from autoweaver.motion_policy.world_board import Snapshot

logger = logging.getLogger(__name__)


class Status(Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    RUNNING = "RUNNING"
    IDLE = "IDLE"


class TreeNode(ABC):
    """Abstract base class for all BT nodes."""

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__
        self.status = Status.IDLE
        self._blackboard: Blackboard
        self._key_mapping: dict[str, str] = {}
        self._snapshot: Snapshot | None = None
        self._exception: BaseException | None = None

    def tick(self, snapshot: Snapshot | None = None) -> Status:
        if snapshot is not None:
            self._snapshot = snapshot

        try:
            if self.status == Status.IDLE:
                self.status = self.on_start()
            elif self.status == Status.RUNNING:
                self.status = self.on_running()
        except Exception as e:
            logger.exception("node '%s' raised", self.name)
            self._exception = e
            self.status = Status.FAILURE

        result = self.status
        if result != Status.RUNNING:
            self.reset()

        return result

    @property
    def snapshot(self) -> Snapshot:
        if self._snapshot is None:
            raise RuntimeError(
                f"node '{self.name}' accessed snapshot outside of a tick — "
                "snapshot is only available during on_start / on_running"
            )
        return self._snapshot

    @abstractmethod
    def on_start(self) -> Status: ...

    @abstractmethod
    def on_running(self) -> Status: ...

    def on_halted(self) -> None:
        pass

    def halt(self) -> None:
        if self.status == Status.RUNNING:
            self.on_halted()
            self.status = Status.IDLE
        self._snapshot = None

    def reset(self) -> None:
        self.status = Status.IDLE
        self._snapshot = None

    def set_blackboard(
        self,
        blackboard: Blackboard,
        key_mapping: dict[str, str] | None = None,
    ) -> None:
        self._blackboard = blackboard
        if key_mapping:
            self._key_mapping = key_mapping

    def get_input(self, key: str) -> Any:
        mapped = self._key_mapping.get(key, key)
        return self._blackboard.read(mapped)

    def set_output(self, key: str, value: Any) -> None:
        mapped = self._key_mapping.get(key, key)
        self._blackboard.write(mapped, value, self.name)

    # --- Operator DSL ---

    def __rshift__(self, other: TreeNode) -> TreeNode:
        """a >> b → Sequence([a, b])"""
        from autoweaver.motion_policy.nodes.control.sequence import Sequence

        left = self._flatten_children(Sequence)
        right = other._flatten_children(Sequence)
        seq = Sequence(left + right)
        seq._was_explicit = False
        return seq

    def __or__(self, other: TreeNode) -> TreeNode:
        """a | b → Fallback([a, b])"""
        from autoweaver.motion_policy.nodes.control.fallback import Fallback

        left = self._flatten_children(Fallback)
        right = other._flatten_children(Fallback)
        fb = Fallback(left + right)
        fb._was_explicit = False
        return fb

    def __and__(self, other: TreeNode) -> TreeNode:
        """a & b → Sequence([a, b]) for condition composition"""
        from autoweaver.motion_policy.nodes.control.sequence import Sequence

        left = self._flatten_children(Sequence)
        right = other._flatten_children(Sequence)
        seq = Sequence(left + right)
        seq._was_explicit = False
        return seq

    def __invert__(self) -> TreeNode:
        """~a → Inverter(a)"""
        from autoweaver.motion_policy.nodes.decorator.inverter import Inverter

        return Inverter(child=self)

    def premise(self, action: TreeNode) -> TreeNode:
        """cond.premise(action) → Premise([cond, action])"""
        from autoweaver.motion_policy.nodes.control.premise import Premise

        return Premise([self, action])

    def timeout(self, seconds: float) -> TreeNode:
        from autoweaver.motion_policy.nodes.decorator.timeout import Timeout

        return Timeout(seconds=seconds, child=self)

    def retry(self, max_attempts: int) -> TreeNode:
        from autoweaver.motion_policy.nodes.decorator.retry import Retry

        return Retry(max_attempts=max_attempts, child=self)

    def repeat(self, count: int) -> TreeNode:
        from autoweaver.motion_policy.nodes.decorator.repeat import Repeat

        return Repeat(count=count, child=self)

    def force_success(self) -> TreeNode:
        from autoweaver.motion_policy.nodes.decorator.force_success import ForceSuccess

        return ForceSuccess(child=self)

    def _flatten_children(self, node_type: type) -> list[TreeNode]:
        """Flatten chained operators: a >> b >> c → Sequence([a, b, c])."""
        if isinstance(self, node_type) and not self._was_explicit:
            return list(self.children)
        return [self]

    def _children_list(self) -> list[TreeNode]:
        return [self]
