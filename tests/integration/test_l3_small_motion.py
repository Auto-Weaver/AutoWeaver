"""L3 — small physical motion at low speed.

What this exercises:
  - joint direction sign (+5° on J1: which way?)
  - controller deceleration profile timing
  - feedback continuity during motion
  - tolerance for "settled" detection

Risk: LOW. J1 +5° at speed=10 (10% of max). Operator must hold e-stop.
The arm WILL move ~5-10cm at the end-effector.

Pre-requisite: arm enabled, in open space, away from obstacles.
"""
from __future__ import annotations

import time

import pytest


@pytest.mark.integration
def test_move_j_completes_to_small_offset(real_dobot):
    arm, board = real_dobot
    time.sleep(0.3)

    snap = board.snapshot()
    assert snap["dobot1.enabled"] is True, "arm not enabled — operator must enable first"

    start = list(snap["dobot1.joint"])
    target = list(start)
    target[0] += 5.0   # J1 +5 degrees
    print()
    print(f"  start joint   : {tuple(round(x, 2) for x in start)}")
    print(f"  target joint  : {tuple(round(x, 2) for x in target)}")

    # ---- forward leg ----
    print(f"  → issuing move_j(target, speed=10)")
    t0 = time.time()
    gid = arm.move_j(target, speed=10)
    print(f"  goal_id={gid}, RPC returned in {(time.time() - t0) * 1000:.1f}ms")

    # wait for running=True (or skip if controller is fast)
    deadline = time.time() + 1.0
    saw_running = False
    while time.time() < deadline:
        if board.snapshot()["dobot1.running"]:
            saw_running = True
            break
        time.sleep(0.02)
    print(f"  running=True observed: {saw_running}")

    # wait for completion
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if not board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.05)

    final = board.snapshot()["dobot1.joint"]
    print(f"  final joint   : {tuple(round(x, 2) for x in final)}")

    # J1 should be near target, others unchanged (within 0.5° tolerance)
    j1_err = abs(final[0] - target[0])
    print(f"  J1 error vs target: {j1_err:.3f}°")
    assert j1_err < 0.5, f"J1 didn't reach target: {final[0]} vs {target[0]}"
    for i in range(1, 6):
        drift = abs(final[i] - start[i])
        assert drift < 0.5, f"joint {i} drifted unexpectedly: {drift:.3f}°"

    # ---- return leg ----
    print(f"  → returning to start, speed=10")
    arm.move_j(start, speed=10)
    # wait for running=True (controller takes a moment to accept and start)
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.02)
    # wait for running=False (motion complete)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if not board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.05)
    returned = board.snapshot()["dobot1.joint"]
    print(f"  returned joint: {tuple(round(x, 2) for x in returned)}")
    j1_return_err = abs(returned[0] - start[0])
    assert j1_return_err < 0.5, f"return failed: {returned[0]} vs {start[0]}"
