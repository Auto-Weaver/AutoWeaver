"""TickContext plumbing — tick time reaches every node in the tree.

``Wait`` / ``Timeout`` used to read ``time.monotonic()`` directly, which
made their deadlines depend on how deep in the tree they sat and forced
tests to sleep for real. They now read ``self.now`` — the tick's
``TickContext.timestamp``, injected from ``BTClock.tick_once`` — so the
whole tree agrees on what time it is and tests can hand time in.
"""

import pytest

from autoweaver.motion_policy.batch import Batch
from autoweaver.motion_policy.nodes.control.sequence import Sequence
from autoweaver.motion_policy.nodes.decorator.force_success import ForceSuccess
from autoweaver.motion_policy.nodes.decorator.timeout import Timeout
from autoweaver.motion_policy.nodes.leaf.wait import Wait
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.base import TickContext
from autoweaver.worker.clock import BTClock


def _ctx(timestamp: float, tick_id: int = 0, dt: float = 0.02) -> TickContext:
    return TickContext(tick_id=tick_id, timestamp=timestamp, dt=dt)


class _RunningForever(TreeNode):
    def __init__(self, name: str = ""):
        super().__init__(name=name)
        self.ticks = 0

    def on_start(self) -> Status:
        self.ticks += 1
        return Status.RUNNING

    def on_running(self) -> Status:
        self.ticks += 1
        return Status.RUNNING


class _NowRecordingLeaf(TreeNode):
    def __init__(self, name: str = ""):
        super().__init__(name=name)
        self.seen_now: list[float] = []
        self.seen_tick_ids: list[int] = []

    def on_start(self) -> Status:
        self.seen_now.append(self.now)
        self.seen_tick_ids.append(self.tick_ctx.tick_id)
        return Status.SUCCESS

    def on_running(self) -> Status:
        return Status.SUCCESS


# --- Wait uses tick time -------------------------------------------------


def test_wait_uses_injected_tick_time_not_wall_clock():
    """No real sleeping: time only moves because we say it does."""
    leaf = Wait(seconds=1.0)

    assert leaf.tick(None, _ctx(100.0, tick_id=0)) == Status.RUNNING
    assert leaf.tick(None, _ctx(100.5, tick_id=1)) == Status.RUNNING
    assert leaf.tick(None, _ctx(100.99, tick_id=2)) == Status.RUNNING
    assert leaf.tick(None, _ctx(101.0, tick_id=3)) == Status.SUCCESS


def test_wait_does_not_expire_when_tick_time_stands_still():
    leaf = Wait(seconds=0.5)
    assert leaf.tick(None, _ctx(50.0)) == Status.RUNNING
    # Same tick timestamp replayed — real wall clock has advanced, tick
    # time has not, so the wait must not fire.
    for _ in range(5):
        assert leaf.tick(None, _ctx(50.0)) == Status.RUNNING


def test_wait_zero_seconds_succeeds_immediately():
    leaf = Wait(seconds=0.0)
    assert leaf.tick(None, _ctx(7.0)) == Status.SUCCESS


# --- Timeout uses tick time ---------------------------------------------


def test_timeout_fires_on_injected_tick_time():
    child = _RunningForever()
    node = Timeout(seconds=1.0, child=child)

    assert node.tick(None, _ctx(200.0, tick_id=0)) == Status.RUNNING
    assert node.tick(None, _ctx(200.9, tick_id=1)) == Status.RUNNING
    assert node.tick(None, _ctx(201.5, tick_id=2)) == Status.FAILURE
    # The child was halted, not ticked, on the tick that blew the budget.
    assert child.ticks == 2


def test_timeout_does_not_fire_when_tick_time_stands_still():
    child = _RunningForever()
    node = Timeout(seconds=0.1, child=child)
    for i in range(10):
        assert node.tick(None, _ctx(300.0, tick_id=i)) == Status.RUNNING
    assert child.ticks == 10


def test_timeout_passes_child_success_through_before_deadline():
    leaf = Wait(seconds=0.5)
    node = Timeout(seconds=10.0, child=leaf)
    assert node.tick(None, _ctx(400.0)) == Status.RUNNING
    assert node.tick(None, _ctx(400.6)) == Status.SUCCESS


# --- self.now without a ctx raises --------------------------------------


def test_now_raises_when_no_tick_context():
    leaf = _NowRecordingLeaf(name="lonely")
    with pytest.raises(RuntimeError, match="accessed now outside of a tick"):
        _ = leaf.now


def test_tick_ctx_raises_when_no_tick_context():
    leaf = _NowRecordingLeaf(name="lonely")
    with pytest.raises(RuntimeError, match="accessed tick_ctx outside of a tick"):
        _ = leaf.tick_ctx


def test_wait_without_ctx_fails_loudly_instead_of_falling_back():
    """No silent fallback to wall clock: the node turns the RuntimeError
    into FAILURE via the base-class exception guard."""
    leaf = Wait(seconds=1.0)
    assert leaf.tick() == Status.FAILURE
    assert isinstance(leaf._exception, RuntimeError)


def test_tick_ctx_cleared_on_reset_and_halt():
    leaf = _RunningForever()
    leaf.tick(None, _ctx(1.0))
    assert leaf._tick_ctx is not None
    leaf.halt()
    assert leaf._tick_ctx is None

    leaf.tick(None, _ctx(2.0))
    leaf.reset()
    assert leaf._tick_ctx is None


# --- ctx propagates down the tree ---------------------------------------


def test_ctx_propagates_through_sequence():
    a = _NowRecordingLeaf(name="a")
    b = _NowRecordingLeaf(name="b")
    seq = Sequence([a, b])
    seq.tick(None, _ctx(500.0, tick_id=17))

    assert a.seen_now == [500.0]
    assert b.seen_now == [500.0]
    assert a.seen_tick_ids == [17]
    assert b.seen_tick_ids == [17]


def test_ctx_propagates_through_decorator():
    leaf = _NowRecordingLeaf()
    fs = ForceSuccess(child=leaf)
    fs.tick(None, _ctx(600.0, tick_id=3))
    assert leaf.seen_now == [600.0]
    assert leaf.seen_tick_ids == [3]


def test_ctx_propagates_through_nested_control_and_decorators():
    leaf = _NowRecordingLeaf(name="deep")
    tree = Sequence([ForceSuccess(child=Timeout(seconds=10.0, child=Sequence([leaf])))])
    tree.tick(None, _ctx(700.0, tick_id=42))
    assert leaf.seen_now == [700.0]
    assert leaf.seen_tick_ids == [42]


def test_every_node_in_one_tick_sees_the_same_now():
    leaves = [_NowRecordingLeaf(name=f"n{i}") for i in range(5)]
    seq = Sequence(leaves)
    seq.tick(None, _ctx(800.0))
    assert {leaf.seen_now[0] for leaf in leaves} == {800.0}


# --- end to end through BTClock -----------------------------------------


def test_clock_hands_the_tick_context_to_the_tree():
    leaf = _NowRecordingLeaf(name="clocked")
    clock = BTClock(WorldBoard())
    clock.submit(Batch(lambda: leaf, name="probe"))

    ctx = clock.tick_once()

    assert leaf.seen_now == [ctx.timestamp]
    assert leaf.seen_tick_ids == [ctx.tick_id]


def test_wait_under_the_clock_does_not_blow_up_on_missing_now():
    """Guards the clock→Batch→tree wiring: a missing ctx would surface as
    FAILURE (the exception guard swallows it), not as an error."""
    batch = Batch(lambda: Wait(seconds=60.0), name="waiter")
    clock = BTClock(WorldBoard())
    clock.submit(batch)

    assert clock.tick_once() is not None
    assert batch.tree.status == Status.RUNNING
    assert batch.result is None
