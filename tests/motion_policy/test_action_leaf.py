from __future__ import annotations

from autoweaver.motion_policy.nodes.leaf.action_leaf import ActionLeaf, GoalId
from autoweaver.motion_policy.nodes.node import Status


class FakeArm:
    """Records every method call for verification."""

    def __init__(self, name: str = "arm"):
        self.name = name
        self.calls: list[tuple] = []
        self._goal_counter = 0

    def move_j(self, target) -> GoalId:
        self._goal_counter += 1
        self.calls.append(("move_j", self._goal_counter, target))
        return self._goal_counter

    def halt(self, goal_id: GoalId) -> None:
        self.calls.append(("halt", goal_id))


class _MoveJ(ActionLeaf):
    def __init__(self, arm: FakeArm, target):
        super().__init__(arm)
        self.target = target
        self.steps_to_finish = 2
        self._step = 0

    def on_start(self) -> Status:
        self._goal_id = self.device.move_j(self.target)
        self._step = 0
        return Status.RUNNING

    def on_running(self) -> Status:
        self._step += 1
        if self._step >= self.steps_to_finish:
            return Status.SUCCESS
        return Status.RUNNING


def test_on_start_sends_goal_returns_running():
    arm = FakeArm()
    leaf = _MoveJ(arm, target=(1, 2, 3))
    status = leaf.tick()
    assert status == Status.RUNNING
    assert arm.calls == [("move_j", 1, (1, 2, 3))]
    assert leaf._goal_id == 1


def test_on_halted_calls_device_halt_with_goal_id():
    arm = FakeArm()
    leaf = _MoveJ(arm, target=(0, 0, 0))
    leaf.tick()
    leaf.halt()
    assert ("halt", 1) in arm.calls
    # _goal_id is cleared after halt so a stale halt doesn't fire twice
    assert leaf._goal_id is None


def test_on_halted_noop_when_no_goal_in_flight():
    arm = FakeArm()
    leaf = _MoveJ(arm, target=(0, 0, 0))
    leaf.halt()
    assert arm.calls == []


def test_running_to_success_no_halt_call():
    arm = FakeArm()
    leaf = _MoveJ(arm, target=(0, 0, 0))
    leaf.steps_to_finish = 1
    assert leaf.tick() == Status.RUNNING
    assert leaf.tick() == Status.SUCCESS
    halt_calls = [c for c in arm.calls if c[0] == "halt"]
    assert halt_calls == []


def test_reset_clears_goal_id():
    arm = FakeArm()
    leaf = _MoveJ(arm, target=(0, 0, 0))
    leaf.tick()
    assert leaf._goal_id == 1
    leaf.reset()
    assert leaf._goal_id is None
