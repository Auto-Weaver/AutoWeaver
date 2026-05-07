"""Dobot controller state constants.

Source: Dobot TCP/IP V4.6 documentation (files/Dobot TCP_IP二次开发接口文档V4.6.0_2024.12.26_cn.pdf)

Two unrelated state spaces live here:
  - RobotMode  — controller state machine (RobotMode() command, also in feedback struct)
  - SafetyState — independent safety subsystem state

Keep this module dependency-free; it's pure constants used both by the
runtime driver and by tests / business code that need to reason about state.
"""
from __future__ import annotations


# --- RobotMode (controller state machine) ---
# Returned by RobotMode() and present in the 30004 feedback struct as "RobotMode".
ROBOT_MODE_NO_CONTROLLER = -1   # synthetic: feedback hasn't arrived yet
ROBOT_MODE_INIT = 1             # 上电初始化中 — initialization may take ~10s after PowerOn()
ROBOT_MODE_BRAKE_OPEN = 2       # 抱闸松开 — brake released; not safe to RequestControl
ROBOT_MODE_DISABLED = 4         # 下使能 — safe to RequestControl + EnableRobot
ROBOT_MODE_ENABLE = 5           # 使能空闲 — must DisableRobot first to RequestControl
ROBOT_MODE_BACKDRIVE = 6        # 拖拽模式 — manual drag; not safe
ROBOT_MODE_RUNNING = 7          # 运行中 — executing motion
ROBOT_MODE_RECORDING = 8        # 单次运动中
ROBOT_MODE_ERROR = 9            # 报警 — needs ClearError() before re-acquiring
ROBOT_MODE_PAUSE = 10           # 暂停
ROBOT_MODE_JOG = 11             # 手自动模式（示教器接管）


def robot_mode_name(mode: int) -> str:
    """Human-readable name for a RobotMode value, for logs and error messages."""
    return _ROBOT_MODE_NAMES.get(mode, f"UNKNOWN({mode})")


_ROBOT_MODE_NAMES = {
    ROBOT_MODE_NO_CONTROLLER: "NO_CONTROLLER",
    ROBOT_MODE_INIT: "INIT",
    ROBOT_MODE_BRAKE_OPEN: "BRAKE_OPEN",
    ROBOT_MODE_DISABLED: "DISABLED",
    ROBOT_MODE_ENABLE: "ENABLE",
    ROBOT_MODE_BACKDRIVE: "BACKDRIVE",
    ROBOT_MODE_RUNNING: "RUNNING",
    ROBOT_MODE_RECORDING: "RECORDING",
    ROBOT_MODE_ERROR: "ERROR",
    ROBOT_MODE_PAUSE: "PAUSE",
    ROBOT_MODE_JOG: "JOG",
}


# Modes from which `RequestControl()` is allowed (PDF section 2.1, RequestControl).
ROBOT_MODES_ALLOW_TCP_HANDOVER = frozenset({
    ROBOT_MODE_DISABLED,
})

# Modes that mean "there's pending motion / a paused project blocking handover";
# Stop() clears the motion queue and returns the controller to an idle-enabled
# state from which DisableRobot → RequestControl can proceed.
ROBOT_MODES_NEED_STOP = frozenset({
    ROBOT_MODE_PAUSE,
    ROBOT_MODE_RUNNING,
    ROBOT_MODE_RECORDING,
})
