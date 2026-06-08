"""Tests for ServoLeaf — the BT leaf closing the IBVS look-then-move loop.

These exercise the whole loop body against a synthetic image-space plant
wired through the WorldBoard, plus the freshness gate and the terminal
classifications. No hardware, no real perception.
"""

from __future__ import annotations

import numpy as np

from autoweaver.motion_policy.blackboard import Blackboard
from autoweaver.motion_policy.nodes.leaf.servo_leaf import ServoLeaf
from autoweaver.motion_policy.nodes.node import Status
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.servo.controller import ServoController
from autoweaver.servo.interaction import ConstantInteractionMatrix


# --- a synthetic image-space plant living on the WorldBoard ----------------


class _Plant:
    """Toy plant: tweezer tip moves in pixels under commanded velocity.

    error = tip_px - target_px. A move command (relative XY velocity in
    "mm") displaces the tip in pixels via L: tip_px += L_plant @ velocity.
    With L_plant == the controller's L, one exact step nulls the error.

    Drives the WorldBoard exactly like the real wiring:
      - publishes vision.tip_px / vision.target_px / vision.frame_id
      - accepts "move" notes and, on delivery, applies them + bumps frame.
    """

    def __init__(self, board: WorldBoard, L_plant: np.ndarray,
                 tip0, target, *, gain_plant: float = 1.0):
        self._board = board
        self._L = L_plant
        self._gain = gain_plant
        self._tip = np.array(tip0, dtype=np.float64)
        self._target = np.array(target, dtype=np.float64)
        self._frame = 0

        board.declare_state("vision.tip_px", np.ndarray, writer="vision")
        board.declare_state("vision.target_px", np.ndarray, writer="vision")
        board.declare_state("vision.frame_id", int, writer="vision")
        board.declare_state("arm.last_completed_id", int, writer="arm")
        board.post_state("arm.last_completed_id", 0, writer="arm")
        board.accept_notes("arm", "move", dict, self._on_move)
        self._publish()  # frame 1

    def _publish(self) -> None:
        self._frame += 1
        self._board.post_state("vision.tip_px", self._tip.copy(), writer="vision")
        self._board.post_state("vision.target_px", self._target.copy(), writer="vision")
        self._board.post_state("vision.frame_id", self._frame, writer="vision")

    def _on_move(self, payload: dict) -> None:
        v = np.asarray(payload["velocity"], dtype=np.float64)
        rid = payload["__request_id__"]
        self._tip = self._tip + self._gain * (self._L @ v)
        self._board.post_state("arm.last_completed_id", int(rid), writer="arm")
        self._publish()  # a new frame becomes available after the move


def _error_fn(snapshot):
    tip = np.asarray(snapshot.get("vision.tip_px"), dtype=np.float64)
    target = np.asarray(snapshot.get("vision.target_px"), dtype=np.float64)
    return tip - target, tip


def _command_fn(velocity, _bb, _snap):
    return {"velocity": np.asarray(velocity, dtype=np.float64)}


def _make_leaf(board, **ctrl_kw):
    defaults = dict(
        gain=1.0, deadband=1.0, max_step=None,
        max_iterations=50, divergence_ratio=None,
    )
    defaults.update(ctrl_kw)
    controller = ServoController(ConstantInteractionMatrix(np.eye(2)), **defaults)
    leaf = ServoLeaf(
        world_board=board,
        controller=controller,
        target="arm",
        note_name="move",
        error_fn=_error_fn,
        command_fn=_command_fn,
        frame_id_key="vision.frame_id",
    )
    leaf.set_blackboard(Blackboard())
    return leaf


def _run(board, leaf, max_ticks=200):
    """Drive the clock-like loop: deliver notes, then tick, until terminal."""
    for _ in range(max_ticks):
        board.deliver_notes()
        status = leaf.tick(board.snapshot())
        if status in (Status.SUCCESS, Status.FAILURE):
            return status
    return Status.RUNNING


def test_closed_loop_converges_to_target():
    board = WorldBoard()
    plant = _Plant(board, L_plant=np.eye(2), tip0=[100.0, 50.0],
                   target=[10.0, 10.0], gain_plant=1.0)
    leaf = _make_leaf(board, gain=0.4, deadband=1.0)
    assert _run(board, leaf) == Status.SUCCESS
    # Tip ended within the deadband of the target.
    final_err = np.linalg.norm(plant._tip - plant._target)
    assert final_err <= 1.0


def test_already_aligned_succeeds_immediately():
    board = WorldBoard()
    _Plant(board, L_plant=np.eye(2), tip0=[10.5, 10.0], target=[10.0, 10.0])
    leaf = _make_leaf(board, deadband=2.0)
    # First fresh frame is already within deadband → SUCCESS on first look.
    board.deliver_notes()
    assert leaf.tick(board.snapshot()) == Status.SUCCESS


def test_holds_running_when_no_fresh_frame():
    board = WorldBoard()
    _Plant(board, L_plant=np.eye(2), tip0=[100.0, 0.0], target=[0.0, 0.0])
    leaf = _make_leaf(board, gain=0.4)

    # Tick 1: acts on frame 1, dispatches a move → RUNNING, phase MOVING.
    assert leaf.tick(board.snapshot()) == Status.RUNNING
    # Without delivering the move note, frame_id is unchanged. More ticks
    # must stay RUNNING and NOT dispatch again (still waiting for the move).
    for _ in range(5):
        assert leaf.tick(board.snapshot()) == Status.RUNNING


def test_freshness_gate_does_not_act_twice_on_same_frame():
    """The leaf must not issue two commands for one perception frame."""
    board = WorldBoard()
    seen: list[int] = []
    plant = _Plant(board, L_plant=np.eye(2), tip0=[100.0, 0.0],
                   target=[0.0, 0.0])

    # Wrap the plant's move handler to count commands per frame.
    orig = plant._on_move
    def counting(payload):
        seen.append(plant._frame)
        orig(payload)
    board._note_acceptors[("arm", "move")].on_receive = counting

    leaf = _make_leaf(board, gain=0.4)
    # Drive a few full cycles.
    for _ in range(20):
        board.deliver_notes()
        if leaf.tick(board.snapshot()) in (Status.SUCCESS, Status.FAILURE):
            break
    # Each command was issued against a distinct frame id — no duplicates.
    assert len(seen) == len(set(seen))


def test_diverges_to_failure():
    """A plant whose response is sign-flipped vs the model → error grows."""
    board = WorldBoard()
    # Controller assumes L = I, but the plant moves the opposite way, so
    # each "correction" doubles the error → divergence guard trips.
    _Plant(board, L_plant=-np.eye(2), tip0=[10.0, 0.0], target=[0.0, 0.0])
    leaf = _make_leaf(board, gain=1.0, deadband=0.5, divergence_ratio=3.0)
    assert _run(board, leaf) == Status.FAILURE


def test_exhausts_to_failure():
    """Too few iterations allowed to reach the target → EXHAUSTED → FAILURE."""
    board = WorldBoard()
    _Plant(board, L_plant=np.eye(2), tip0=[100.0, 0.0], target=[0.0, 0.0])
    # Tiny gain + tight iteration cap → can't converge in time.
    leaf = _make_leaf(board, gain=0.05, deadband=1.0,
                      max_iterations=3, divergence_ratio=None)
    assert _run(board, leaf) == Status.FAILURE


def test_reset_makes_leaf_rerunnable():
    board = WorldBoard()
    plant = _Plant(board, L_plant=np.eye(2), tip0=[50.0, 0.0],
                   target=[0.0, 0.0])
    leaf = _make_leaf(board, gain=0.5, deadband=1.0)
    assert _run(board, leaf) == Status.SUCCESS

    # New target; the same leaf should run a fresh episode after reset.
    # (tick auto-resets on terminal, but call explicitly to be sure.)
    leaf.reset()
    plant._target = np.array([80.0, -40.0])
    plant._publish()
    assert _run(board, leaf) == Status.SUCCESS
    assert np.linalg.norm(plant._tip - plant._target) <= 1.0
