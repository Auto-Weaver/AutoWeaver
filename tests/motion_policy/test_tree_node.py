from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.world_board import WorldBoard


class _BoomLeaf(TreeNode):
    def on_start(self) -> Status:
        raise RuntimeError("boom")

    def on_running(self) -> Status:
        return Status.RUNNING


class _RecordingLeaf(TreeNode):
    def __init__(self, name: str = ""):
        super().__init__(name=name)
        self.seen_snapshots = []

    def on_start(self) -> Status:
        self.seen_snapshots.append(self.snapshot)
        return Status.SUCCESS

    def on_running(self) -> Status:
        return Status.SUCCESS


def test_node_exception_caught_and_converted_to_failure():
    leaf = _BoomLeaf()
    status = leaf.tick()
    assert status == Status.FAILURE
    assert isinstance(leaf._exception, RuntimeError)


def test_snapshot_passed_to_leaf():
    board = WorldBoard()
    board.register("k", int, writer="w")
    board.write("k", 42, writer="w")
    snap = board.snapshot()

    leaf = _RecordingLeaf()
    leaf.tick(snap)
    assert leaf.seen_snapshots == [snap]


def test_snapshot_propagates_through_sequence():
    from autoweaver.motion_policy.nodes.control.sequence import Sequence

    board = WorldBoard()
    board.register("k", int, writer="w")
    board.write("k", 7, writer="w")
    snap = board.snapshot()

    a = _RecordingLeaf(name="a")
    b = _RecordingLeaf(name="b")
    seq = Sequence([a, b])
    seq.tick(snap)
    assert a.seen_snapshots == [snap]
    assert b.seen_snapshots == [snap]


def test_snapshot_propagates_through_decorator():
    from autoweaver.motion_policy.nodes.decorator.force_success import ForceSuccess

    board = WorldBoard()
    board.register("k", int, writer="w")
    board.write("k", 1, writer="w")
    snap = board.snapshot()

    leaf = _RecordingLeaf()
    fs = ForceSuccess(child=leaf)
    fs.tick(snap)
    assert leaf.seen_snapshots == [snap]


def test_snapshot_cleared_on_reset():
    leaf = _RecordingLeaf()
    leaf._snapshot = "stale"  # type: ignore[assignment]
    leaf.reset()
    assert leaf._snapshot is None


def test_snapshot_property_raises_when_not_set():
    import pytest

    leaf = _RecordingLeaf()
    with pytest.raises(RuntimeError):
        _ = leaf.snapshot
