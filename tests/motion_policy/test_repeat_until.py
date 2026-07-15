from autoweaver.motion_policy.blackboard import Blackboard, BoardView
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


def _wire(ru: RepeatUntil, bb: Blackboard | None = None) -> Blackboard:
    bb = bb or Blackboard()
    ru.set_blackboard(bb)
    return bb


def test_child_reruns_until_cond_true():
    board = _board(False)
    child = _CountingChild()
    ru = RepeatUntil(lambda s, b: bool(s.get("loop.done")), child)
    _wire(ru)

    assert ru.tick(board.snapshot()) == Status.RUNNING
    assert child.starts == 1
    # cond still false → child re-runs (on_start called again).
    assert ru.tick(board.snapshot()) == Status.RUNNING
    assert child.starts == 2

    board.post_state("loop.done", True, writer="ext")
    assert ru.tick(board.snapshot()) == Status.SUCCESS
    assert child.starts == 3


def test_cond_reads_blackboard_counter():
    # Exit condition depends on a blackboard loop counter, invisible to the
    # snapshot — the whole point of handing cond a BoardView.
    child = _CountingChild()
    ru = RepeatUntil(lambda s, b: b.read("flow.pokes", 0) >= 2, child)
    bb = _wire(ru)
    board = _board(False)

    assert ru.tick(board.snapshot()) == Status.RUNNING  # counter 0 → keep going
    bb.set_initial("flow.pokes", 1)
    assert ru.tick(board.snapshot()) == Status.RUNNING  # counter 1 → keep going
    bb.set_initial("flow.pokes", 2)
    assert ru.tick(board.snapshot()) == Status.SUCCESS  # counter 2 → exit


def test_board_view_is_read_only():
    view = BoardView(Blackboard())
    assert hasattr(view, "read")
    assert not hasattr(view, "write")
    assert not hasattr(view, "set_initial")
    assert not hasattr(view, "register_key")


def test_child_failure_propagates():
    board = _board(False)
    child = _CountingChild(result=Status.FAILURE)
    called = {"cond": 0}

    def cond(s, b):
        called["cond"] += 1
        return True

    ru = RepeatUntil(cond, child)
    _wire(ru)
    assert ru.tick(board.snapshot()) == Status.FAILURE
    # cond is not consulted when the child fails.
    assert called["cond"] == 0


def test_running_child_does_not_evaluate_cond():
    board = _board(True)
    child = _CountingChild(result=Status.RUNNING)
    called = {"cond": 0}

    def cond(s, b):
        called["cond"] += 1
        return True

    ru = RepeatUntil(cond, child)
    _wire(ru)
    assert ru.tick(board.snapshot()) == Status.RUNNING
    assert called["cond"] == 0


def test_at_most_one_pass_per_tick():
    board = _board(True)
    child = _CountingChild()
    ru = RepeatUntil(lambda s, b: bool(s.get("loop.done")), child)
    _wire(ru)
    # cond is true, but only one child pass completes this tick.
    assert ru.tick(board.snapshot()) == Status.SUCCESS
    assert child.starts == 1
