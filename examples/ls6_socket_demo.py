"""LS6 socket BT demo — drive EpsonLS6SocketWorker through a BT tree.

End-to-end smoke test for the autoweaver 0.10 socket path on a real
LS6 arm + RC90B SPEL+ socket server. Wires the smallest reasonable
graph that exercises the full chain:

    BT tree (Sequence of NotifyAndWait leaves)
        ↓ pass_note
    WorldBoard
        ↓ accept_notes wrapper (PerceptionWorker — synchronous)
    EpsonLS6SocketWorker
        ↓ EpsonLS6Socket (TCP/JSON)
        ↓
    RC90B SPEL+ BgMain  →  arm motion

What it does
------------
Three commands in sequence:

    1. ``task_finish`` — protocol smoke, no motion. SPEL+ replies
                          ``task_finished``.
    2. ``pick(x, y, z, u)`` — real motion: SPEL+ does its full
                              pick gesture (Z<40 safety lift then
                              fly-to-(x,y,z+30,u) then descend-to-
                              (x,y,z,u)). Coordinates are the arm's
                              own current Local-2-frame pose read off
                              the operator's RC+ teach display, so the
                              motion is effectively a self-loop —
                              start at P, end at P, with the safety
                              lift + over + descend in between.
    3. ``task_finish`` — close out, stops SPEL+ task timer.

That's it — no Nova2, no Nova5, no perception. If this exits with
SUCCESS the autoweaver socket Worker + BTClock + NotifyAndWait + SPEL+
all talk correctly through the full motion path.

Operationally
-------------
1. On RC90B: SPEL+ BgMain in Run, listening on TCP 5000.
2. From the IPC:

       ~/.local/bin/uv run python -m examples.ls6_socket_demo

   Total runtime ~5-8s (the pick motion is the long part).
"""

from __future__ import annotations

import logging
import time
from typing import List

from autoweaver.device.arm.epson_ls6 import EpsonLS6SocketWorker
from autoweaver.motion_policy.batch import Batch
from autoweaver.motion_policy.nodes.control.sequence import Sequence
from autoweaver.motion_policy.nodes.leaf.notify_and_wait import NotifyAndWait
from autoweaver.motion_policy.nodes.node import TreeNode
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.clock import BTClock

logger = logging.getLogger(__name__)


RC90B_IP = "192.168.5.99"
RC90B_PORT = 5000
WORKER_NAME = "ls6_1"

# Arm pose read off the RC+ teach display, in SPEL+ Local 2 frame.
# Using "current = target" makes the pick a safe self-loop: SPEL+ still
# runs the full gesture (safety lift since z<40, fly above, descend
# back) but the start and end positions match. Verify on the screen
# before launching.
PICK_X = 61.918
PICK_Y = -110.986
PICK_Z = 22.061
PICK_U = 60.236


def build_demo_tree(board: WorldBoard) -> TreeNode:
    """task_finish → pick → task_finish."""
    children: List[TreeNode] = [
        NotifyAndWait(
            world_board=board, target=WORKER_NAME, note_name="task_finish",
            payload={}, name="LS6.task_finish_pre",
        ),
        NotifyAndWait(
            world_board=board, target=WORKER_NAME, note_name="pick",
            payload={"x": PICK_X, "y": PICK_Y, "z": PICK_Z, "u": PICK_U},
            name=f"LS6.pick({PICK_X:.3f},{PICK_Y:.3f},{PICK_Z:.3f},{PICK_U:.3f})",
        ),
        NotifyAndWait(
            world_board=board, target=WORKER_NAME, note_name="task_finish",
            payload={}, name="LS6.task_finish_post",
        ),
    ]
    return Sequence(children, name="ls6_socket_demo_sequence")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info(
        "LS6 socket BT demo — connecting to RC90B at %s:%d (worker '%s')",
        RC90B_IP, RC90B_PORT, WORKER_NAME,
    )

    board = WorldBoard()
    clock = BTClock(world_board=board, hz=50)

    worker = EpsonLS6SocketWorker(
        ip=RC90B_IP, port=RC90B_PORT, name=WORKER_NAME,
    )
    # attach_worker runs on_attach + on_start, which opens the socket
    # and sends the required initial start handshake. If anything in
    # there fails, attach_worker re-raises and the script exits non-zero
    # — exactly what we want for a smoke test.
    clock.attach_worker(worker)

    # The Batch takes the *factory*, not the tree: the program is the
    # function that builds a tree, because tree nodes carry run state.
    batch = Batch(lambda: build_demo_tree(board), name="ls6_socket_demo")
    clock.submit(batch)

    logger.info("Starting BTClock at 50Hz — Ctrl+C to stop")
    try:
        # The business owns the loop; the framework has no blocking wait.
        while batch.result is None:
            clock.tick_once()
            time.sleep(0.02)  # 50 Hz
        result = batch.result
        logger.info(
            "Demo finished — %s (final status: %s)",
            result.reason.value, result.final_status,
        )
    except KeyboardInterrupt:
        logger.warning("Ctrl+C — halting")
    finally:
        clock.shutdown()


if __name__ == "__main__":
    main()
