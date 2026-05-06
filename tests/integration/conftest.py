"""Integration test fixtures — real Dobot hardware required.

Tests in this directory only run when AUTOWEAVER_DOBOT_IP is set in the
environment. Without it, all tests are skipped automatically — this prevents
accidental hardware tests in CI.
"""
from __future__ import annotations

import os
import time

import pytest

from autoweaver.device.arm.dobot import Dobot
from autoweaver.motion_policy.world_board import WorldBoard


@pytest.fixture
def real_dobot():
    """Connected + TCP-controlled + enabled Dobot.

    The fixture takes full responsibility for controller handover so tests
    don't need to know about Dobot's mode state machine.

    On exit: releases TCP control (DisableRobot) and closes sockets.
    """
    ip = os.environ.get("AUTOWEAVER_DOBOT_IP")
    if not ip:
        pytest.skip("AUTOWEAVER_DOBOT_IP not set; skipping integration test")

    arm = Dobot(ip=ip, name="dobot1")
    board = WorldBoard()
    arm.register_outputs(board)
    arm.start()
    try:
        time.sleep(0.5)  # let a few feedback frames land
        arm.acquire_control()
        yield arm, board
    finally:
        arm.stop()


@pytest.fixture
def connected_dobot():
    """Just connected — no TCP handover, no enable. For probing controller state."""
    ip = os.environ.get("AUTOWEAVER_DOBOT_IP")
    if not ip:
        pytest.skip("AUTOWEAVER_DOBOT_IP not set; skipping integration test")

    arm = Dobot(ip=ip, name="dobot1")
    board = WorldBoard()
    arm.register_outputs(board)
    arm.start()
    try:
        time.sleep(0.5)
        yield arm, board
    finally:
        # use stop() but skip release_control — caller may be in a state where
        # release would error
        try:
            arm.stop()
        except Exception:
            pass

