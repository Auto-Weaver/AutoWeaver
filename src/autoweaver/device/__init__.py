"""Physical devices grouped by category.

Layout:
    device/arm/    -- robot arms (Dobot, Epson, ...)
    device/sensor/ -- sensors

Devices are imported explicitly from their own modules; this package does
not re-export anything to avoid coupling unrelated subpackages.
"""
