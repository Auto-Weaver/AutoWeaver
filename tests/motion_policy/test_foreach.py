import pytest

from autoweaver.motion_policy.blackboard import Blackboard
from autoweaver.motion_policy.nodes.decorator.foreach import ForEach
from autoweaver.motion_policy.nodes.node import Status, TreeNode


class _FixedChild(TreeNode):
    """Child whose per-tick result is settable; records blackboard reads and halts."""

    def __init__(self, key: str, result: Status = Status.SUCCESS):
        super().__init__()
        self._key = key
        self.result = result
        self.seen: list = []
        self.halted = 0

    def on_start(self) -> Status:
        self.seen.append(self._blackboard.read(self._key))
        return self.result

    def on_running(self) -> Status:
        return self.result

    def on_halted(self) -> None:
        self.halted += 1


def _foreach(items, result=Status.SUCCESS):
    child = _FixedChild("item", result)
    fe = ForEach("item", items, child)
    fe.set_blackboard(Blackboard())
    return fe, child


def test_empty_items_succeeds_immediately():
    fe, child = _foreach([])
    assert fe.tick() == Status.SUCCESS
    assert child.seen == []


def test_one_item_per_tick_and_key_written_in_order():
    fe, child = _foreach([10, 20, 30])
    assert fe.tick() == Status.RUNNING
    assert fe.tick() == Status.RUNNING
    assert fe.tick() == Status.SUCCESS
    # child observed each item, exactly one per tick, in order.
    assert child.seen == [10, 20, 30]


def test_child_failure_aborts_and_idx_reset():
    fe, child = _foreach([1, 2, 3], result=Status.FAILURE)
    assert fe.tick() == Status.FAILURE
    # terminal status auto-resets the decorator: idx back to 0.
    assert fe._idx == 0
    # re-runnable after failure: flip child to succeed, full traversal restarts at item 0.
    child.result = Status.SUCCESS
    child.seen.clear()
    assert fe.tick() == Status.RUNNING
    assert fe.tick() == Status.RUNNING
    assert fe.tick() == Status.SUCCESS
    assert child.seen == [1, 2, 3]


def test_terminal_then_retick_restarts_from_first_item():
    fe, child = _foreach([7, 8])
    assert fe.tick() == Status.RUNNING
    assert fe.tick() == Status.SUCCESS
    assert child.seen == [7, 8]
    # ticking a completed ForEach starts a fresh traversal (auto-reset contract).
    assert fe.tick() == Status.RUNNING
    assert fe.tick() == Status.SUCCESS
    assert child.seen == [7, 8, 7, 8]


def test_halt_propagates_to_running_child():
    fe, child = _foreach([1, 2], result=Status.RUNNING)
    assert fe.tick() == Status.RUNNING
    fe.halt()
    # DecoratorNode.halt recurses into the child; child was RUNNING so on_halted fires.
    assert child.halted == 1


def test_non_foreach_writer_rejected():
    bb = Blackboard()
    fe = ForEach("item", [1], _FixedChild("item"))
    fe.set_blackboard(bb)
    with pytest.raises(PermissionError):
        bb.write("item", 99, "outsider")
