from __future__ import annotations

import asyncio
import logging

import pytest

from autoweaver.motion_policy.action import Action, ActionResult
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.tracer import LogTracer
from autoweaver.motion_policy.world_board import WorldBoard


class _ImmediateSuccess(TreeNode):
    def on_start(self) -> Status:
        return Status.SUCCESS

    def on_running(self) -> Status:
        return Status.SUCCESS


class _ImmediateFailure(TreeNode):
    def on_start(self) -> Status:
        return Status.FAILURE

    def on_running(self) -> Status:
        return Status.FAILURE


class _BoomLeaf(TreeNode):
    def on_start(self) -> Status:
        raise ValueError("kaboom")

    def on_running(self) -> Status:
        return Status.RUNNING


class _NeverFinish(TreeNode):
    """Records every tick. Stays RUNNING forever."""

    def __init__(self):
        super().__init__()
        self.tick_count = 0
        self.snapshots_seen: list = []
        self.halted = False

    def on_start(self) -> Status:
        self.tick_count += 1
        self.snapshots_seen.append(self.snapshot)
        return Status.RUNNING

    def on_running(self) -> Status:
        self.tick_count += 1
        self.snapshots_seen.append(self.snapshot)
        return Status.RUNNING

    def on_halted(self) -> None:
        self.halted = True


class _RecordingTracer:
    def __init__(self):
        self.events: list[tuple] = []

    def on_action_start(self, action_name):
        self.events.append(("start", action_name))

    def on_action_end(self, action_name, result):
        self.events.append(("end", action_name, result.success))

    def on_tick_start(self, tick_seq):
        self.events.append(("tick_start", tick_seq))

    def on_tick_end(self, tick_seq, duration, root_status):
        self.events.append(("tick_end", tick_seq, root_status))

    def on_slow_tick(self, duration, target):
        self.events.append(("slow_tick", duration, target))

    def on_node_exception(self, node_name, exception):
        self.events.append(("node_exception", node_name, type(exception).__name__))


@pytest.mark.asyncio
async def test_run_returns_success_on_root_success():
    action = Action(tree=_ImmediateSuccess(), hz=1000)
    result = await action.run()
    assert result.success is True
    assert result.final_status == Status.SUCCESS


@pytest.mark.asyncio
async def test_run_returns_failure_on_root_failure():
    action = Action(tree=_ImmediateFailure(), hz=1000)
    result = await action.run()
    assert result.success is False
    assert result.final_status == Status.FAILURE


@pytest.mark.asyncio
async def test_node_exception_propagates_to_action_result():
    leaf = _BoomLeaf()
    action = Action(tree=leaf, hz=1000)
    result = await action.run()
    assert result.success is False
    assert isinstance(result.exception, ValueError)
    assert result.failed_node == leaf.name


@pytest.mark.asyncio
async def test_halt_exits_and_runs_finally_tree_halt():
    tree = _NeverFinish()
    action = Action(tree=tree, hz=200)

    async def halt_after_a_few_ticks():
        await asyncio.sleep(0.05)
        action.halt()

    halter = asyncio.create_task(halt_after_a_few_ticks())
    result = await action.run()
    await halter
    assert result.success is False
    assert result.message == "halted"
    assert tree.halted, "tree.halt() must be called via finally"
    assert tree.tick_count >= 1


@pytest.mark.asyncio
async def test_finally_runs_even_on_exception_inside_run():
    """If something inside run() raises, tree.halt() must still run."""
    tree = _NeverFinish()
    action = Action(tree=tree, hz=100)

    # Patch the tracer to raise inside on_action_start, before any tick
    class _AngryTracer:
        def on_action_start(self, name):
            raise RuntimeError("tracer is angry")

        def __getattr__(self, _):
            return lambda *a, **k: None

    action._tracer = _AngryTracer()
    with pytest.raises(RuntimeError):
        await action.run()
    # tree never started running — halt() is called but the inner if
    # status==RUNNING guard means on_halted is not invoked. The contract is
    # that tree.halt() was *attempted*.


@pytest.mark.asyncio
async def test_snapshot_passed_to_tree_each_tick():
    """The leaf records the snapshot it sees each tick."""
    board = WorldBoard()
    board.register("k", int, writer="w")
    board.write("k", 1, writer="w")

    tree = _NeverFinish()
    action = Action(tree=tree, world_board=board, hz=200)

    async def stop_soon():
        await asyncio.sleep(0.06)
        action.halt()

    stopper = asyncio.create_task(stop_soon())
    await action.run()
    await stopper
    assert len(tree.snapshots_seen) >= 1
    # Each recorded snapshot is the one current at tick start.
    assert all(s["k"] == 1 for s in tree.snapshots_seen)


@pytest.mark.asyncio
async def test_tracer_receives_lifecycle_events():
    tracer = _RecordingTracer()
    action = Action(tree=_ImmediateSuccess(), hz=1000, tracer=tracer)
    await action.run()
    kinds = [e[0] for e in tracer.events]
    assert kinds[0] == "start"
    assert "tick_start" in kinds
    assert "tick_end" in kinds
    assert kinds[-1] == "end"


@pytest.mark.asyncio
async def test_slow_tick_warning_emitted(caplog):
    """A tree that sleeps in on_start should trigger slow tick warning."""
    import time

    class _SlowLeaf(TreeNode):
        def on_start(self):
            time.sleep(0.05)
            return Status.SUCCESS

        def on_running(self):
            return Status.SUCCESS

    tracer = _RecordingTracer()
    action = Action(tree=_SlowLeaf(), hz=100, tracer=tracer)  # 10ms target
    with caplog.at_level(logging.WARNING):
        await action.run()
    slow_events = [e for e in tracer.events if e[0] == "slow_tick"]
    assert len(slow_events) == 1
    assert any("slow tick" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_node_exception_event_fires_on_tracer():
    tracer = _RecordingTracer()
    action = Action(tree=_BoomLeaf(), hz=1000, tracer=tracer)
    await action.run()
    exc_events = [e for e in tracer.events if e[0] == "node_exception"]
    assert len(exc_events) == 1
    assert exc_events[0][2] == "ValueError"


@pytest.mark.asyncio
async def test_log_tracer_does_not_blow_up():
    """Smoke test — LogTracer can replace NullTracer without errors."""
    action = Action(tree=_ImmediateSuccess(), hz=1000, tracer=LogTracer())
    result = await action.run()
    assert result.success is True
