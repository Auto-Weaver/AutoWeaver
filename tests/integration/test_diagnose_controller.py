"""Diagnose current controller state — read RobotMode and try transitions."""
from __future__ import annotations

import os
import time

import pytest

from autoweaver.device.arm._dobot_sdk import DobotApiDashboard, DobotApiFeedBack
from autoweaver.device.arm.dobot_states import robot_mode_name


@pytest.mark.integration_safe
def test_inspect_current_state():
    ip = os.environ.get("AUTOWEAVER_DOBOT_IP")
    if not ip:
        pytest.skip("AUTOWEAVER_DOBOT_IP not set")

    fb = DobotApiFeedBack(ip, 30004)
    time.sleep(0.5)
    frame = fb.feedBackData()
    f0 = frame[0]
    mode = int(f0["RobotMode"])
    print()
    print(f"  current RobotMode    : {mode} ({robot_mode_name(mode)})")
    print(f"  EnableStatus         : {bool(f0['EnableStatus'])}")
    print(f"  RunningStatus        : {bool(f0['RunningStatus'])}")
    print(f"  ErrorStatus          : {bool(f0['ErrorStatus'])}")
    print(f"  SafetyState          : {int(f0['SafetyState'])}")
    print(f"  CurrentCommandId     : {int(f0['CurrentCommandId'])}")

    fb.close()


@pytest.mark.integration_safe
def test_try_full_handover_with_poweron():
    """Full handover sequence including PowerOn, with longer waits."""
    ip = os.environ.get("AUTOWEAVER_DOBOT_IP")
    if not ip:
        pytest.skip("AUTOWEAVER_DOBOT_IP not set")

    dash = DobotApiDashboard(ip, 29999)
    fb = DobotApiFeedBack(ip, 30004)
    print()

    def read_mode():
        time.sleep(0.2)
        f = fb.feedBackData()
        return int(f[0]["RobotMode"])

    print(f"  start mode      : {robot_mode_name(read_mode())}")

    resp = dash.RequestControl()
    print(f"  RequestControl  : {resp!r}")
    print(f"  mode after      : {robot_mode_name(read_mode())}")

    resp = dash.PowerOn()
    print(f"  PowerOn         : {resp!r}")
    # PDF says PowerOn takes ~10s. Watch transitions
    for i in range(15):
        m = read_mode()
        print(f"  mode after {i+1}s : {robot_mode_name(m)}")
        if m == 4 or m == 5:  # DISABLED or ENABLE
            break

    resp = dash.EnableRobot()
    print(f"  EnableRobot     : {resp!r}")
    for i in range(8):
        m = read_mode()
        print(f"  mode after {i+1}s : {robot_mode_name(m)}")
        if m == 5:  # ENABLE
            break

    # cleanup: leave it disabled
    dash.DisableRobot()
    dash.close()
    fb.close()
