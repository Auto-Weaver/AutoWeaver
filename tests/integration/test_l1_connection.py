"""L1 — connection + feedback. Arm does NOT move.

What this test exercises:
  - vendor SDK can parse current firmware feedback
  - 1440-byte numpy struct field offsets match
  - 7 WorldBoard keys are populated as expected
  - feedback frames arrive at ~125Hz (8ms period)

Risk: zero (no motion command issued).
"""
from __future__ import annotations

import time

import pytest


@pytest.mark.integration_safe
def test_can_receive_feedback(real_dobot):
    arm, board = real_dobot
    time.sleep(1.0)  # give the feedback thread time to land ~125 frames
    snap = board.snapshot()

    # we expect ~125 frames in 1s; allow generous floor for jitter
    assert snap.seq > 50, f"only got {snap.seq} feedback frames in 1s — feedback thread starved?"

    pose = snap["dobot1.pose"]
    joint = snap["dobot1.joint"]

    # if SDK parsing is wrong, these stay at the (0,0,0,0,0,0) defaults
    assert pose != (0.0,) * 6, "pose still all zeros — SDK feedback parsing likely broken"
    assert joint != (0.0,) * 6, "joint still all zeros"

    # human-readable output for the operator
    print()
    print(f"  feedback frames in 1s : {snap.seq}")
    print(f"  pose (X,Y,Z,Rx,Ry,Rz) : {pose}")
    print(f"  joint (J1..J6)        : {joint}")
    print(f"  running               : {snap['dobot1.running']}")
    print(f"  enabled               : {snap['dobot1.enabled']}")
    print(f"  error                 : {snap['dobot1.error']}")
    print(f"  safety_state          : {snap['dobot1.safety_state']}")
    print(f"  current_cmd_id        : {snap['dobot1.current_cmd_id']}")
