"""Vendored Dobot TCP/IP Python V4 SDK. See README.md for provenance.

Only the symbols our `device/arm/dobot.py` wrapper needs are re-exported.
Anything else lives inside `dobot_api` and should be accessed there
explicitly — that signals it is SDK-internal and may go away on update.
"""

from autoweaver.device.arm._dobot_sdk.dobot_api import (
    DobotApiDashboard,
    DobotApiFeedBack,
    MyType,
)

__all__ = ["DobotApiDashboard", "DobotApiFeedBack", "MyType"]
