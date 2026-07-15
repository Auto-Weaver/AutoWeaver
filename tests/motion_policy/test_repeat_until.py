from autoweaver.motion_policy.nodes.decorator.repeat_until import RepeatUntil
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.world_board import WorldBoard


class _CountingChild(TreeNode):
    """Counts on_start invocations; per-tick result is settable."""

    def __init__(self, result: Status = Status.SUCCESS):
        super().__init__()
        self.result = result
        self.starts = 0

    def on_start(self) -> Status:
        self.starts += 1
        return self.result

    def on_running(self) -> Status:
        return self.result


def _board(done: bool):
    board = WorldBoard()
    board.declare_state("loop.done", bool, writer="ext")
    board.post_state("loop.done", done, writer="ext")
    return board


def test_child_reruns_until_cond_true():
    board = _board(False)
    child = _CountingChild()
    ru = RepeatUntil(lambda s: bool(s.get("loop.done")), child)

    assert ru.tick(board.snapshot()) == Status.RUNNING
    assert child.starts == 1
    # cond still false → child re-runs (on_start called again).
    assert ru.tick(board.snapshot()) == Status.RUNNING
    assert child.starts == 2

    board.post_state("loop.done", True, writer="ext")
    assert ru.tick(board.snapshot()) == Status.SUCCESS
    assert child.starts == 3


def test_child_failure_propagates():
    board = _board(False)
    child = _CountingChild(result=Status.FAILURE)
    called = {"cond": 0}

    def cond(s):
        called["cond"] += 1
        return True

    ru = RepeatUntil(cond, child)
    assert ru.tick(board.snapshot()) == Status.FAILURE
    # cond is not consulted when the child fails.
    assert called["cond"] == 0


def test_running_child_does_not_evaluate_cond():
    board = _board(True)
    child = _CountingChild(result=Status.RUNNING)
    called = {"cond": 0}

    def cond(s):
        called["cond"] += 1
        return True

    ru = RepeatUntil(cond, child)
    assert ru.tick(board.snapshot()) == Status.RUNNING
    assert called["cond"] == 0


def test_at_most_one_pass_per_tick():
    board = _board(True)
    child = _CountingChild()
    ru = RepeatUntil(lambda s: bool(s.get("loop.done")), child)
    # cond is true, but only one child pass completes this tick.
    assert ru.tick(board.snapshot()) == Status.SUCCESS
    assert child.starts == 1
