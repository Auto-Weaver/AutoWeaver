"""Tests for DahengCamera white balance — mode + hard-coded manual ratios.

No hardware and no gxipy: the SDK module is faked at ``daheng.gx`` and the device
handle is a stand-in that reproduces the three shapes that actually bite —

  * ``hasattr(cam, feature)`` is True for every feature even when the device does
    not implement it (gxipy's ``Device.__init__`` attaches them unconditionally),
    so only ``is_implemented`` / ``is_writable`` tell the truth;
  * ``EnumFeature.get()`` returns ``(int, "SYMBOLIC")``, ``FloatFeature.get()``
    returns a bare float;
  * ``BalanceRatio`` is ONE selector-driven register, not three features.
"""

from __future__ import annotations

import logging

import pytest

from autoweaver.sensor.camera import daheng as daheng_module
from autoweaver.sensor.camera.base import CameraConfig


# --- fake gxipy ------------------------------------------------------------ #


class _GxAutoEntry:
    OFF = 0
    CONTINUOUS = 1
    ONCE = 2


class _GxBalanceRatioSelectorEntry:
    RED = 0
    GREEN = 1
    BLUE = 2


class _FakeGx:
    GxAutoEntry = _GxAutoEntry
    GxBalanceRatioSelectorEntry = _GxBalanceRatioSelectorEntry


# --- fake device ----------------------------------------------------------- #


class _Feature:
    """Base fake feature — implemented/writable are answered by the device, not
    by attribute presence."""

    def __init__(self, *, implemented: bool = True, writable: bool = True):
        self._implemented = implemented
        self._writable = writable

    def is_implemented(self):
        return self._implemented

    def is_readable(self):
        return self._implemented

    def is_writable(self):
        return self._implemented and self._writable


class _EnumFeature(_Feature):
    def __init__(self, value=0, **kw):
        super().__init__(**kw)
        self.value = value
        self.writes = []

    def set(self, enum_value):
        if not self.is_writable():
            raise RuntimeError("not writable")
        self.value = enum_value
        self.writes.append(enum_value)

    def get(self):
        return (self.value, {0: "Off", 1: "Continuous", 2: "Once"}.get(self.value, "?"))


class _BalanceRatio(_Feature):
    """One register behind a selector, with the device's 1/512 quantisation."""

    STEP = 1.0 / 512.0

    def __init__(self, selector: _EnumFeature, **kw):
        super().__init__(**kw)
        self._selector = selector
        self.values = {0: 1.0, 1: 1.0, 2: 1.0}
        self.write_order = []

    def set(self, float_value):
        if not self.is_writable():
            raise RuntimeError("not writable")
        channel = self._selector.value
        quantised = round(float(float_value) / self.STEP) * self.STEP
        self.values[channel] = quantised
        self.write_order.append((channel, quantised))

    def get(self):
        return self.values[self._selector.value]


class _FakeDevice:
    """Every feature attached unconditionally — exactly like gxipy's Device."""

    def __init__(self, *, wb_auto_implemented=True, ratio_implemented=True,
                 selector_implemented=True):
        self.BalanceWhiteAuto = _EnumFeature(implemented=wb_auto_implemented)
        self.BalanceRatioSelector = _EnumFeature(implemented=selector_implemented)
        self.BalanceRatio = _BalanceRatio(self.BalanceRatioSelector,
                                          implemented=ratio_implemented)


@pytest.fixture
def camera(monkeypatch):
    """A ``DahengCamera`` factory that never touches the SDK or a device."""
    monkeypatch.setattr(daheng_module, "gx", _FakeGx)

    def _make(config: CameraConfig, device: _FakeDevice | None = None):
        cam = daheng_module.DahengCamera(config)
        cam._cam = device if device is not None else _FakeDevice()
        return cam

    return _make


# --- mode ------------------------------------------------------------------ #


def test_mode_off_sets_the_off_entry(camera):
    cam = camera(CameraConfig(white_balance_mode="off"))
    cam._configure_white_balance()
    assert cam._cam.BalanceWhiteAuto.value == _GxAutoEntry.OFF


def test_yaml_bare_off_boolean_raises_instead_of_falling_back_to_auto(camera):
    """``white_balance_mode: off`` unquoted in YAML is the boolean False.

    The old ``(mode or "auto")`` fallback turned that into CONTINUOUS — white
    balance running non-stop, the exact opposite of the intent, silently.
    """
    cam = camera(CameraConfig(white_balance_mode=False))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="quotes"):
        cam._configure_white_balance()
    assert cam._cam.BalanceWhiteAuto.writes == []


def test_none_mode_raises_too(camera):
    cam = camera(CameraConfig(white_balance_mode=None))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cam._configure_white_balance()


def test_unknown_mode_raises(camera):
    cam = camera(CameraConfig(white_balance_mode="sometimes"))
    with pytest.raises(ValueError, match="unknown white_balance_mode"):
        cam._configure_white_balance()


def test_missing_balance_white_auto_feature_warns_and_names_it(camera, caplog):
    """hasattr() is True even here — only is_writable() knows."""
    device = _FakeDevice(wb_auto_implemented=False)
    assert hasattr(device, "BalanceWhiteAuto")
    cam = camera(CameraConfig(white_balance_mode="off"), device)
    with caplog.at_level(logging.WARNING):
        cam._configure_white_balance()
    assert "BalanceWhiteAuto" in caplog.text


# --- manual ratios --------------------------------------------------------- #


_RATIOS = dict(white_balance_red=2.1719, white_balance_green=1.0, white_balance_blue=1.8320)


def test_ratios_are_written_per_channel_through_the_selector(camera):
    cam = camera(CameraConfig(white_balance_mode="off", **_RATIOS))
    cam._configure_white_balance()

    ratio = cam._cam.BalanceRatio
    step = _BalanceRatio.STEP
    assert abs(ratio.values[_GxBalanceRatioSelectorEntry.RED] - 2.1719) <= step
    assert abs(ratio.values[_GxBalanceRatioSelectorEntry.GREEN] - 1.0) <= step
    assert abs(ratio.values[_GxBalanceRatioSelectorEntry.BLUE] - 1.8320) <= step
    # One selector write per channel, i.e. three "select then write" pairs.
    assert [c for c, _ in ratio.write_order] == [
        _GxBalanceRatioSelectorEntry.RED,
        _GxBalanceRatioSelectorEntry.GREEN,
        _GxBalanceRatioSelectorEntry.BLUE,
    ]


def test_quantisation_alone_does_not_trip_the_readback_warning(camera, caplog):
    """The device rounds to 1/512; that must not be reported as a failed write."""
    cam = camera(CameraConfig(white_balance_mode="off", **_RATIOS))
    with caplog.at_level(logging.WARNING):
        cam._configure_white_balance()
    assert "read back" not in caplog.text


def test_readback_mismatch_warns(camera, caplog):
    device = _FakeDevice()

    def _swallow(_value):
        pass  # device accepts the write and keeps its old value

    device.BalanceRatio.set = _swallow  # type: ignore[method-assign]
    cam = camera(CameraConfig(white_balance_mode="off", **_RATIOS), device)
    with caplog.at_level(logging.WARNING):
        cam._configure_white_balance()
    assert "read back" in caplog.text


def test_no_ratios_leaves_the_register_alone(camera):
    cam = camera(CameraConfig(white_balance_mode="off"))
    cam._configure_white_balance()
    assert cam._cam.BalanceRatio.write_order == []


def test_partial_ratio_set_is_a_config_error(camera):
    cam = camera(CameraConfig(white_balance_mode="off", white_balance_red=2.17))
    with pytest.raises(ValueError, match="set of three"):
        cam._configure_white_balance()


def test_partial_ratio_set_raises_before_touching_the_device(camera):
    """A config error must not leave the camera half-configured."""
    cam = camera(CameraConfig(white_balance_mode="off", white_balance_red=2.17))
    with pytest.raises(ValueError):
        cam._configure_white_balance()
    assert cam._cam.BalanceWhiteAuto.writes == []


def test_ratios_under_an_automatic_mode_warn_but_still_apply(camera, caplog):
    """Do not quietly rewrite the user's mode — say what will happen, then obey."""
    cam = camera(CameraConfig(white_balance_mode="once", **_RATIOS))
    with caplog.at_level(logging.WARNING):
        cam._configure_white_balance()
    assert "overwrite" in caplog.text
    assert cam._cam.BalanceWhiteAuto.value == _GxAutoEntry.ONCE
    assert cam._cam.BalanceRatio.write_order  # applied anyway


def test_unwritable_balance_ratio_warns_and_names_the_feature(camera, caplog):
    device = _FakeDevice(ratio_implemented=False)
    cam = camera(CameraConfig(white_balance_mode="off", **_RATIOS), device)
    with caplog.at_level(logging.WARNING):
        cam._configure_white_balance()
    assert "BalanceRatio" in caplog.text
    assert device.BalanceRatio.write_order == []


def test_unwritable_selector_warns_and_names_the_feature(camera, caplog):
    device = _FakeDevice(selector_implemented=False)
    cam = camera(CameraConfig(white_balance_mode="off", **_RATIOS), device)
    with caplog.at_level(logging.WARNING):
        cam._configure_white_balance()
    assert "BalanceRatioSelector" in caplog.text


# --- config plumbing ------------------------------------------------------- #


def test_ratios_travel_with_the_observation_conditions():
    """The gains multiply into the gray a detector thresholds against, so they
    belong to "how this frame was acquired"."""
    from autoweaver.sensor.camera.base import CameraBase

    for field_name in ("white_balance_red", "white_balance_green", "white_balance_blue"):
        assert field_name in CameraBase._CONDITION_FIELDS


def test_ratios_default_to_none():
    cfg = CameraConfig()
    assert (cfg.white_balance_red, cfg.white_balance_green, cfg.white_balance_blue) == (
        None, None, None)
