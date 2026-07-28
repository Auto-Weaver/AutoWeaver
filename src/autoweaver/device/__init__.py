"""Physical devices grouped by category.

Layout:
    device/arm/    -- robot arms (Dobot, Epson, ...)

Sensors do NOT live here. They sit under ``autoweaver/sensor/`` (cameras at
``sensor/camera/``), because that package holds framework contracts —
``CameraConfig``, the projection model, ``_build_observation`` — not vendor
drivers. The symmetry to aim for is ``sensor/camera/daheng.py`` alongside
``device/arm/dobot/``, not a ``device/sensor/`` mirror. See EVO-011 and the
retracted item in ``docs/next/006-dobot-arm-mainline.md``.

Devices are imported explicitly from their own modules; this package does
not re-export anything to avoid coupling unrelated subpackages.
"""
