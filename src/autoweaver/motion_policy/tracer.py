from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from autoweaver.motion_policy.action import ActionResult
    from autoweaver.motion_policy.nodes.node import Status


logger = logging.getLogger(__name__)


class ActionTracer(Protocol):
    """Lifecycle observability hooks for Action.run().

    The minimum set covers Action lifecycle, slow-tick detection, and node
    exceptions — enough to answer "what happened?" when something goes
    wrong, without per-node trace overhead. Per-node tracing (full BT
    trajectory for replay / RL data) is intentionally not included; see
    north_star/world-board-as-rl-trajectory.md.
    """

    def on_action_start(self, action_name: str) -> None: ...
    def on_action_end(self, action_name: str, result: ActionResult) -> None: ...
    def on_tick_start(self, tick_seq: int) -> None: ...
    def on_tick_end(self, tick_seq: int, duration: float, root_status: Status) -> None: ...
    def on_slow_tick(self, duration: float, target: float) -> None: ...
    def on_node_exception(self, node_name: str, exception: BaseException) -> None: ...


class NullTracer:
    """No-op tracer — production default. Zero overhead."""

    def on_action_start(self, action_name: str) -> None:
        pass

    def on_action_end(self, action_name: str, result: ActionResult) -> None:
        pass

    def on_tick_start(self, tick_seq: int) -> None:
        pass

    def on_tick_end(self, tick_seq: int, duration: float, root_status: Status) -> None:
        pass

    def on_slow_tick(self, duration: float, target: float) -> None:
        pass

    def on_node_exception(self, node_name: str, exception: BaseException) -> None:
        pass


class LogTracer:
    """Emits human-readable log lines — useful during development."""

    def on_action_start(self, action_name: str) -> None:
        logger.info("action '%s' start", action_name)

    def on_action_end(self, action_name: str, result: ActionResult) -> None:
        logger.info(
            "action '%s' end: success=%s message=%s",
            action_name,
            result.success,
            result.message,
        )

    def on_tick_start(self, tick_seq: int) -> None:
        pass

    def on_tick_end(self, tick_seq: int, duration: float, root_status: Status) -> None:
        pass

    def on_slow_tick(self, duration: float, target: float) -> None:
        logger.warning(
            "slow tick: %.1fms (target %.1fms)", duration * 1000, target * 1000
        )

    def on_node_exception(self, node_name: str, exception: BaseException) -> None:
        logger.error(
            "node '%s' raised %s: %s",
            node_name,
            type(exception).__name__,
            exception,
        )
