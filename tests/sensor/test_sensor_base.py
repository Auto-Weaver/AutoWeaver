"""Tests for Sensor base — protocol shape and CameraBase compat."""

from __future__ import annotations

import numpy as np

from autoweaver.sensor.camera.base import CameraBase, CameraConfig
from autoweaver.sensor.camera.mock import MockCamera
from autoweaver.sensor.base import Sensor


def _make_mock_camera() -> MockCamera:
    return MockCamera(CameraConfig(), mode="random", width=64, height=48)


def test_camera_base_is_a_sensor():
    """CameraBase must satisfy the Sensor protocol via inheritance."""
    assert issubclass(CameraBase, Sensor)


def test_mock_camera_is_a_sensor():
    """Concrete MockCamera satisfies Sensor via the CameraBase chain."""
    assert isinstance(_make_mock_camera(), Sensor)


def test_mock_camera_snapshot_returns_image():
    """The new snapshot() entry point works on the mock."""
    cam = _make_mock_camera()
    cam.open()
    try:
        img = cam.snapshot()
        assert isinstance(img, np.ndarray)
        # MockCamera default produces a 3-channel BGR frame.
        assert img.ndim == 3
    finally:
        cam.close()


def test_camera_base_capture_alias_still_works():
    """Backward-compat: cam.capture() still routes to snapshot()."""
    cam = _make_mock_camera()
    cam.open()
    try:
        a = cam.snapshot()
        b = cam.capture()
        assert a.shape == b.shape
    finally:
        cam.close()


def test_camera_base_is_opened_alias():
    """Backward-compat: cam.is_opened() still works."""
    cam = _make_mock_camera()
    cam.open()
    try:
        assert cam.is_open() is True
        assert cam.is_opened() is True
    finally:
        cam.close()
    assert cam.is_open() is False
    assert cam.is_opened() is False
