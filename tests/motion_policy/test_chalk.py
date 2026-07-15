import pytest

from autoweaver.motion_policy.blackboard import Blackboard
from autoweaver.motion_policy.nodes.leaf.chalk import Chalk
from autoweaver.motion_policy.nodes.node import Status
from autoweaver.motion_policy.world_board import WorldBoard


def _wire(chalk: Chalk, bb: Blackboard | None = None) -> Blackboard:
    bb = bb or Blackboard()
    chalk.set_blackboard(bb)
    return bb


def test_inc_from_default_zero_accumulates():
    chalk = Chalk.inc("counter")
    bb = _wire(chalk)
    assert chalk.tick() == Status.SUCCESS
    assert bb.read("counter") == 1
    # single instance re-ticks (auto-reset) and keeps accumulating.
    assert chalk.tick() == Status.SUCCESS
    assert bb.read("counter") == 2


def test_inc_by_custom_step():
    chalk = Chalk.inc("counter", by=5)
    bb = _wire(chalk)
    chalk.tick()
    assert bb.read("counter") == 5


def test_set_constant():
    chalk = Chalk.set("k", 42)
    bb = _wire(chalk)
    chalk.tick()
    assert bb.read("k") == 42


def test_set_callable_reads_snapshot():
    board = WorldBoard()
    board.declare_state("sensor.x", int, writer="ext")
    board.post_state("sensor.x", 7, writer="ext")
    chalk = Chalk.set("k", lambda s: s.get("sensor.x"))
    bb = _wire(chalk)
    chalk.tick(board.snapshot())
    assert bb.read("k") == 7


def test_append_constant_and_callable():
    board = WorldBoard()
    board.declare_state("sensor.x", int, writer="ext")
    board.post_state("sensor.x", 99, writer="ext")

    const = Chalk.append("log", "a")
    bb = _wire(const)
    const.tick()
    const.tick()
    assert bb.read("log") == ["a", "a"]

    dyn = Chalk.append("log", lambda s: s.get("sensor.x"))
    dyn.set_blackboard(bb)  # same key, same "chalk" writer — allowed.
    dyn.tick(board.snapshot())
    assert bb.read("log") == ["a", "a", 99]


def test_general_fn_reads_snapshot_and_current():
    chalk = Chalk("k", lambda snapshot, current: (current or 0) + 10)
    bb = _wire(chalk)
    chalk.tick()
    chalk.tick()
    assert bb.read("k") == 20


def test_multiple_chalk_instances_same_key_coexist():
    bb = Blackboard()
    a = Chalk.inc("k")
    b = Chalk.inc("k", by=2)
    a.set_blackboard(bb)
    b.set_blackboard(bb)  # idempotent same-writer registration, no raise.
    a.tick()
    b.tick()
    assert bb.read("k") == 3


def test_external_writer_rejected():
    chalk = Chalk.inc("k")
    bb = _wire(chalk)
    with pytest.raises(PermissionError):
        bb.write("k", 99, "outsider")


def test_register_key_idempotent_same_writer_conflict_other():
    bb = Blackboard()
    bb.register_key("k", object, "chalk")
    bb.register_key("k", object, "chalk")  # same writer — no raise.
    with pytest.raises(ValueError):
        bb.register_key("k", object, "foreach")  # different writer — conflict.
