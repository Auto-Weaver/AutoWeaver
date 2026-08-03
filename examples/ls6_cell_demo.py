"""LS6 cell demo — EtherCAT path (legacy / diagnostic only).

⚠️  This demo demonstrates the EtherCAT trigger-edge protocol bug,
not a working flow. **Use** ``examples/ls6_socket_demo.py`` for actual
LS6 development.

History (2026-05-18 bench session):

This was the first end-to-end smoke test of the autoweaver 0.9.x
EtherCAT stack on a real LS6 arm. It exposed a race in
motion-runtime's falling-edge piggyback that surfaces under
BTClock-driven sequential dispatch: ``read_scara_status`` can fire
piggyback before SPEL+ has had time to flip ``done`` low for the
queued motion, silently dropping the command. Concretely, this demo
issues 12 motion commands but only the first JUMP physically
completes — commands 2..12 each receive a 1ms-window piggyback
acknowledgement and the rest of the sequence is silently no-op'd.

See ``pluck-hair/docs/error/01-ls6-ethercat-trigger-race.md`` for the
full root cause analysis. The fix path will need a sequence-numbered
ACK in motion-runtime, but that wasn't worth the time relative to the
business v1, so the project moved to the SPEL+ socket-server path
(``ls6_socket_demo.py``).

The demo is kept in-tree for two reasons:

1. **Diagnostic reproducer** — if someone wants to debug the EtherCAT
   race or verify a fix, this is the smallest failing case.
2. **Code reference** for the EtherCAT path's worker construction
   (``EpsonLS6Worker`` via ``RuntimeClient``).

End-to-end smoke test for the 0.9 stack on a real LS6 arm. Wires up
the smallest reasonable graph that exercises the full path:

    BT tree (Sequence of NotifyAndWait leaves)
        ↓ pass_note
    WorldBoard
        ↓ accept_async_notes wrapper
    EpsonLS6Worker (MotionWorker subclass)
        ↓ ScaraGoalBuilder
    RuntimeClient (gRPC)
        ↓
    motion-runtime (Rust)  →  EtherCAT  →  RC90B  →  LS6

What it does
------------
Walks 4 cells along +X (12 mm apart, matching the Nova5 cell pitch
the press-grid BT uses), and at each cell does a "fake pick":

    1. SCARA jump to cell over travel altitude (z_travel)
    2. linear down to pick altitude (z_pick)
    3. settle 0.3 s
    4. linear back up to z_travel

That's it — no Nova2, no Nova5, no perception, no preview. The cell-
to-cell rhythm + the down/up gesture are exactly what the three-arm
tree (src/bt/three_arm_tree.py in pluck-hair) will issue, just
isolated to one worker so failures can only be in the LS6 column.

Starting pose
-------------
Hard-coded to the LS6 pose recorded on the test rig
(x=113.239, y=251.563, z=-55.838, u=60.244) — read off the RC+
controller screen by the operator. Move LS6 close to this pose with
the teach pendant **before** launching the demo; the first action is
a jump from this pose, and the controller has no way to know where it
"thinks" it is until that move completes.

Operationally
-------------
Run on the IPC where motion-runtime is up:

    ~/.local/bin/uv run python -m examples.ls6_cell_demo

Watch the SPEL+ Print Window for trigger / done handshakes; watch the
RC+ Robot Status for live position. Hit Ctrl+C anytime to stop —
the BTClock.shutdown() path will halt the worker cleanly.
"""

from __future__ import annotations

import logging
import time
from typing import List

from autoweaver.device.arm.epson_ls6 import EpsonLS6Worker
from autoweaver.motion_policy.batch import Batch
from autoweaver.motion_policy.nodes.control.sequence import Sequence
from autoweaver.motion_policy.nodes.leaf.notify_and_wait import NotifyAndWait
from autoweaver.motion_policy.nodes.node import TreeNode
from autoweaver.motion_policy.runtime_client import RuntimeClient
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.clock import BTClock

logger = logging.getLogger(__name__)


# Operator-read starting pose (RC+ controller screen, 2026-05-18).
# Update if the rig is re-zeroed or the home position moves.
START_X = 113.239
START_Y = 251.563
START_Z = -55.838
START_U = 60.244

# Cell layout — same as the press-grid BT.
CELL_COUNT = 4
CELL_PITCH_MM = 12.0     # +X step between consecutive cells

# Z gestures.
Z_TRAVEL_OFFSET = 10.0   # travel altitude = START_Z + 10 mm (raised)
Z_PICK = START_Z         # "pick" altitude = back at start z
Z_TRAVEL = START_Z + Z_TRAVEL_OFFSET

# Motion params.
# SPEL+ accepts speed/accel as percentages 0..100 of the controller's
# max — see motion-runtime/contracts/.../contract.yaml. Going above 100
# raises ERR_MOTION_FAILED (SPEL+ `Accel 200,200` rejects). The
# autoweaver EpsonLS6Worker default is accel=200 which is over-range;
# we override here. 50 + 30 was verified on the bench on 2026-05-18.
SPEED = 30
ACCEL = 50
PICK_SETTLE_S = 0.3

# LS6 worker / runtime config.
DEVICE_NAME = "arm"        # matches motion-runtime contract device field
WORKER_NAME = "ls6_1"      # logical name for WorldBoard namespace
RUNTIME_ADDR = "localhost:50051"


def _ls6_jump_payload(x: float, y: float, z: float, u: float) -> dict:
    return {"target": (x, y, z, u), "speed": SPEED, "accel": ACCEL}


def _ls6_linear_payload(x: float, y: float, z: float, u: float) -> dict:
    return {"target": (x, y, z, u), "speed": SPEED, "accel": ACCEL}


def _pick_cycle_at(board: WorldBoard, cell_index: int) -> Sequence:
    """One cell's "fake pick": jump in → drop → settle → lift."""
    x_cell = START_X + cell_index * CELL_PITCH_MM
    label = f"cell{cell_index:02d}"

    travel_pose = (x_cell, START_Y, Z_TRAVEL, START_U)
    pick_pose = (x_cell, START_Y, Z_PICK, START_U)

    # py_trees-style: build leaves with concrete payload dicts; the
    # framework injects __request_id__ when it pass_note's.
    from autoweaver.motion_policy.nodes.leaf.notify_and_wait import NotifyAndWait
    # SCARA jump to cell over travel altitude.
    fly_in = NotifyAndWait(
        world_board=board, target=WORKER_NAME, note_name="jump",
        payload=_ls6_jump_payload(*travel_pose),
        name=f"LS6.jump_to({label})",
    )
    # Straight-line descent to pick altitude.
    descend = NotifyAndWait(
        world_board=board, target=WORKER_NAME, note_name="move_l",
        payload=_ls6_linear_payload(*pick_pose),
        name=f"LS6.descend({label})",
    )
    # Lift back up to travel altitude (also linear so it's symmetric).
    ascend = NotifyAndWait(
        world_board=board, target=WORKER_NAME, note_name="move_l",
        payload=_ls6_linear_payload(*travel_pose),
        name=f"LS6.ascend({label})",
    )

    return Sequence([fly_in, descend, _Sleep(PICK_SETTLE_S, f"settle({label})"),
                     ascend],
                    name=f"PickCycle({label})")


class _Sleep(TreeNode):
    """Inline Sleep leaf — tiny pause for the tweezer to settle."""

    def __init__(self, duration_s: float, name: str = ""):
        super().__init__(name=name or f"Sleep({duration_s:.2f}s)")
        self._duration = float(duration_s)
        self._deadline = 0.0

    def on_start(self):
        from autoweaver.motion_policy.nodes.node import Status
        self._deadline = time.monotonic() + self._duration
        return Status.RUNNING if time.monotonic() < self._deadline else Status.SUCCESS

    def on_running(self):
        from autoweaver.motion_policy.nodes.node import Status
        return Status.SUCCESS if time.monotonic() >= self._deadline else Status.RUNNING

    def reset(self) -> None:
        self._deadline = 0.0
        super().reset()


def build_demo_tree(board: WorldBoard) -> TreeNode:
    """Walk all CELL_COUNT cells once, top-to-bottom, then SUCCESS."""
    children: List[TreeNode] = []
    for i in range(CELL_COUNT):
        children.append(_pick_cycle_at(board, i))
    return Sequence(children, name="ls6_cell_demo_sequence")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.warning(
        "LS6 cell demo — assumes arm is at "
        "(x=%.3f, y=%.3f, z=%.3f, u=%.3f). VERIFY ON RC+ BEFORE LAUNCH.",
        START_X, START_Y, START_Z, START_U,
    )
    logger.info(
        "Cells: %d  pitch=%.1fmm  Z travel=%.3f  Z pick=%.3f  speed=%d",
        CELL_COUNT, CELL_PITCH_MM, Z_TRAVEL, Z_PICK, SPEED,
    )

    client = RuntimeClient(address=RUNTIME_ADDR)
    board = WorldBoard()
    clock = BTClock(world_board=board, hz=50)

    worker = EpsonLS6Worker(
        client=client,
        device_name=DEVICE_NAME,
        name=WORKER_NAME,
        speed=SPEED,
        accel=ACCEL,
    )
    clock.attach_worker(worker)

    # The Batch takes the *factory*, not the tree: the program is the
    # function that builds a tree, because tree nodes carry run state.
    batch = Batch(lambda: build_demo_tree(board), name="ls6_cell_demo")
    clock.submit(batch)

    logger.info("Starting BTClock at 50Hz — Ctrl+C to stop")
    try:
        # The business owns the loop; the framework has no blocking wait.
        while batch.result is None:
            clock.tick_once()
            time.sleep(0.02)  # 50 Hz
        logger.info("Demo batch finished — %s (final status: %s)",
                    batch.result.reason.value, batch.result.final_status)
    except KeyboardInterrupt:
        logger.warning("Ctrl+C — halting")
    finally:
        clock.shutdown()
        client.close()


if __name__ == "__main__":
    main()
