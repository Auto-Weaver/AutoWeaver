from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from autoweaver.motion_policy.batch import BatchResult
    from autoweaver.motion_policy.nodes.node import Status


logger = logging.getLogger(__name__)


class BatchTracer(Protocol):
    """Lifecycle observability hooks for a running ``Batch``.

    The minimum set covers Batch lifecycle, slow-tick detection, and node
    exceptions — enough to answer "what happened?" when something goes
    wrong, without per-node trace overhead. Per-node tracing (full BT
    trajectory for replay / RL data) is intentionally not included; see
    north_star/world-board-as-rl-trajectory.md.

    ``on_batch_begin`` / ``on_batch_finish`` are deliberately *not* named
    ``on_batch_start`` / ``on_batch_end``: ``Worker.on_batch_start`` is a
    different hook, on a different object, with a different signature
    (EVO-014 §10). One name, one meaning.

    ``on_batch_finish`` fires once, when the Batch reaches EXITED — i.e.
    after the teardown tree has run, not when the main tree stopped.
    """

    def on_batch_begin(self, batch_name: str) -> None: ...
    def on_batch_finish(self, batch_name: str, result: BatchResult) -> None: ...
    def on_tick_start(self, tick_seq: int) -> None: ...
    def on_tick_end(self, tick_seq: int, duration: float, root_status: Status) -> None: ...
    def on_slow_tick(self, duration: float, target: float) -> None: ...
    def on_node_exception(self, node_name: str, exception: BaseException) -> None: ...


class NullTracer:
    """No-op tracer — production default. Zero overhead."""

    def on_batch_begin(self, batch_name: str) -> None:
        pass

    def on_batch_finish(self, batch_name: str, result: BatchResult) -> None:
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

    def on_batch_begin(self, batch_name: str) -> None:
        logger.info("batch '%s' begin", batch_name)

    def on_batch_finish(self, batch_name: str, result: BatchResult) -> None:
        logger.info(
            "batch '%s' finish: reason=%s message=%s",
            batch_name,
            result.reason.value,
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
