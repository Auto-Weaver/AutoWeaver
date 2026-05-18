"""Robot arm device implementations, organised by hardware model.

Each subfolder is one arm model. Add a new arm by adding a new
subfolder with ``driver.py`` (vendor wrapper / driver) and ``worker.py``
(BT-facing Worker), plus any model-specific helpers (state constants,
vendored SDK, ...).

Import directly from the model subfolder:

    from autoweaver.device.arm.dobot import Dobot, DobotWorker
    from autoweaver.device.arm.epson_ls6 import EpsonLS6, EpsonLS6Worker

Or from a leaf:

    from autoweaver.device.arm.dobot.driver import Dobot
    from autoweaver.device.arm.dobot.worker import DobotWorker

The ``base`` module hosts the arm protocols (``ArmBase4`` / ``ArmBase6``)
and validation helpers; ``mock`` provides a fake arm useful in tests
and untethered Worker integration runs.
"""
