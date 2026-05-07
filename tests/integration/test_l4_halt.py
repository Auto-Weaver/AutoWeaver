"""L4 — halt mid-motion. Verify the controller actually stops.

What this exercises:
  - dashboard.Stop() actually halts physical motion mid-flight
  - running flag transitions from True back to False after Stop
  - the arm does NOT reach target (Stop short-circuits the trajectory)
  - GoalId stale-halt protection: halt(stale_id) is a no-op

Risk: LOW-MEDIUM. J1 +30° at speed=10 is a real motion (~6s travel,
~30-40cm at the end-effector). Halt is issued ~1.5s in, before motion
completes. Final J1 lands somewhere between start and target — operator
should be ready for an unexpected stop position.

Pre-requisite: arm enabled, in open space, 30+cm clearance from any
obstacle in the J1 sweep direction. Operator hand on e-stop.
"""
from __future__ import annotations

import time

import pytest


@pytest.mark.integration
def test_halt_actually_stops_motion(real_dobot):
    arm, board = real_dobot
    time.sleep(0.3)

    snap = board.snapshot()
    assert snap["dobot1.enabled"] is True
    start = list(snap["dobot1.joint"])
    target = list(start)
    target[0] += 30.0   # big move so we have time to halt
    print()
    print(f"  start joint   : {tuple(round(x, 2) for x in start)}")
    print(f"  target joint  : {tuple(round(x, 2) for x in target)}")

    # ---- issue long move ----
    print(f"  → issuing move_j(target, speed=10)")
    gid = arm.move_j(target, speed=10)
    print(f"  goal_id={gid}")

    # wait for running=True (motion actually started)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.02)
    assert board.snapshot()["dobot1.running"], "motion never started"
    print(f"  running=True observed, motion underway")

    # let it run for ~12s — at speed=10 (~1.76°/s for J1) this puts us
    # roughly 20-22° into the 30° arc, clearly mid-motion and visually
    # observable on the physical arm before we halt.
    time.sleep(12.0)
    mid_joint = board.snapshot()["dobot1.joint"]
    progress_deg = abs(mid_joint[0] - start[0])
    print(f"  mid-flight J1 : {round(mid_joint[0], 2)}° (Δ={round(progress_deg, 2)}°)")
    assert progress_deg > 15.0, f"arm only moved {progress_deg}° in 12s — slower than expected"
    assert progress_deg < 28.0, f"arm essentially finished before halt: Δ={progress_deg}°"

    # ---- halt ----
    print(f"  → halt(goal_id={gid})")
    t0 = time.time()
    arm.halt(gid)
    print(f"  halt() returned in {(time.time() - t0) * 1000:.1f}ms")

    # wait for running=False (deceleration complete)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.02)
    stopped_at = board.snapshot()["dobot1.joint"]
    halt_lag = time.time() - t0
    print(f"  stopped J1    : {round(stopped_at[0], 2)}° "
          f"(stop took ~{halt_lag * 1000:.0f}ms)")
    assert not board.snapshot()["dobot1.running"], "still running 3s after halt"

    # final position should be between start and target — NOT at target
    j1_to_target = abs(stopped_at[0] - target[0])
    assert j1_to_target > 1.0, (
        f"halt didn't short-circuit: stopped at {stopped_at[0]}, target was {target[0]}"
    )
    print(f"  J1 distance from target : {round(j1_to_target, 2)}° (good — halt worked)")

    # report what mode the controller settled in (informative)
    final_mode = board.snapshot()["dobot1.robot_mode"]
    print(f"  RobotMode after halt    : {final_mode}")

    # ---- stale halt is a no-op ----
    print(f"  → stale halt({gid}) (should be ignored)")
    arm.halt(gid)   # already halted — should be silently ignored

    # ---- recover to start ----
    # acquire_control may need to be re-run if Stop dropped us out of ENABLE.
    # Best-effort: try move_j; if it fails, re-acquire and retry once.
    print(f"  → returning to start, speed=10")
    try:
        arm.move_j(start, speed=10)
    except Exception as exc:
        print(f"  move_j after halt raised: {exc}; re-acquiring control")
        arm.acquire_control()
        arm.move_j(start, speed=10)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.02)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if not board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.05)
    returned = board.snapshot()["dobot1.joint"]
    print(f"  returned joint: {tuple(round(x, 2) for x in returned)}")
    j1_return_err = abs(returned[0] - start[0])
    assert j1_return_err < 0.5, f"return failed: {returned[0]} vs {start[0]}"
