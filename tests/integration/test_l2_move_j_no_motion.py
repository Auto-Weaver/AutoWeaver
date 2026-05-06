"""L2 — issue a real MovJ to current position. Arm does NOT move physically.

What this exercises:
  - MovJ command format (coordinate order, coordinateMode value)
  - controller acceptance: ACK comes back, RunningStatus transitions
  - GoalId counter increments

Risk: zero in principle (target = current pose), but a real RPC is sent.
The arm should briefly show running=True then return to running=False.
"""
from __future__ import annotations

import time

import pytest


@pytest.mark.integration_safe
def test_move_j_to_current_position(real_dobot):
    arm, board = real_dobot
    time.sleep(0.3)

    snap = board.snapshot()
    assert snap["dobot1.enabled"] is True, "arm not enabled — operator must enable first"

    current_joint = snap["dobot1.joint"]
    print()
    print(f"  before: joint={current_joint}, running={snap['dobot1.running']}")

    gid = arm.move_j(current_joint)
    print(f"  move_j issued, goal_id={gid}")

    # wait briefly to let the controller acknowledge and respond
    time.sleep(0.5)
    snap_after = board.snapshot()
    print(f"  after 0.5s: running={snap_after['dobot1.running']}, joint={snap_after['dobot1.joint']}")

    # wait until running clears (with timeout)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.05)

    final = board.snapshot()
    print(f"  final: running={final['dobot1.running']}, joint={final['dobot1.joint']}")

    assert not final["dobot1.running"], "arm still running after 5s — controller didn't accept or finish"
    # joint should be unchanged (we sent target=current)
    for i, (c, f) in enumerate(zip(current_joint, final["dobot1.joint"])):
        assert abs(c - f) < 0.5, f"joint {i} drifted: before={c}, after={f}"
