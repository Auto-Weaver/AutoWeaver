"""Tests for Observation — identity, immutability and lineage (EVO-011)."""

from __future__ import annotations

import numpy as np
import pytest

from autoweaver.sensor.camera.observation import CameraObservation
from autoweaver.sensor.observation import Derivation, Observation, PixelTransform


def _image(width: int = 64, height: int = 48) -> np.ndarray:
    return np.arange(height * width * 3, dtype=np.uint8).reshape(height, width, 3)


def _observation(**overrides) -> CameraObservation:
    kwargs = dict(id=7, source="nest", captured_at=1.5, data=_image())
    kwargs.update(overrides)
    return CameraObservation(**kwargs)


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

def test_observation_fields_are_frozen():
    """Reassigning a field must fail — the record of a sampling event is fixed."""
    obs = _observation()
    with pytest.raises(Exception):
        obs.id = 9  # type: ignore[misc]


def test_payload_is_read_only():
    """Constructing an Observation takes ownership: the array becomes read-only,
    so 'immutable' is enforced rather than merely documented."""
    obs = _observation()
    assert obs.data.flags.writeable is False
    with pytest.raises(ValueError):
        obs.data[0, 0, 0] = 1


def test_non_array_payload_passes_through():
    """A scalar reading (a future pressure sensor) must not trip the array path."""
    obs = Observation(id=1, source="pressure", captured_at=0.0, data=3.14)
    assert obs.data == 3.14


# --------------------------------------------------------------------------- #
# lineage
# --------------------------------------------------------------------------- #

def test_fresh_observation_is_root():
    obs = _observation()
    assert obs.is_root
    assert obs.derived_from is None
    assert obs.lineage() == ()
    assert obs.root is obs


def test_root_observation_has_no_parent_mapping():
    """to_parent() on a root raises rather than silently returning (u, v) —
    quietly succeeding would hide the caller's mistake."""
    with pytest.raises(ValueError):
        _observation().to_parent(1, 2)


def test_crop_maps_coordinates_back_to_root():
    """The whole point of lineage: a coordinate measured inside a crop converts
    itself back, so business code never has to remember the crop origin."""
    obs = _observation()
    cropped = obs.crop(10, 5, 20, 12)

    assert cropped.size == (20, 12)
    assert cropped.to_root(0, 0) == (10.0, 5.0)
    assert cropped.to_root(3, 4) == (13.0, 9.0)
    assert cropped.root is obs


def test_crop_is_a_view_not_a_copy():
    """A 9 MB frame cannot afford a copy per reframing step."""
    obs = _observation()
    cropped = obs.crop(4, 4, 8, 8)
    assert np.shares_memory(obs.data, cropped.data)


def test_crop_inherits_the_sampling_identity():
    """A crop is a different *view* of the same shutter, so id / source /
    captured_at carry over — that is what makes id usable as a freshness gate."""
    obs = _observation()
    cropped = obs.crop(1, 1, 4, 4)
    assert (cropped.id, cropped.source, cropped.captured_at) == (
        obs.id, obs.source, obs.captured_at,
    )


def test_crop_carries_conditions_and_projection():
    """Reframing does not change the exposure that produced these pixels."""
    projection = object()
    obs = _observation(conditions={"exposure_time": 12000}, projection=projection)
    cropped = obs.crop(0, 0, 8, 8)
    assert cropped.conditions == {"exposure_time": 12000}
    assert cropped.projection is projection


def test_crop_rejects_out_of_bounds():
    """Bounds are validated, not clamped: a silently smaller region would
    corrupt every coordinate derived from it."""
    obs = _observation(data=_image(32, 32))
    with pytest.raises(ValueError):
        obs.crop(20, 0, 20, 8)
    with pytest.raises(ValueError):
        obs.crop(-1, 0, 4, 4)
    with pytest.raises(ValueError):
        obs.crop(0, 0, 0, 4)


def test_resize_maps_coordinates_back():
    obs = _observation(data=_image(64, 48))
    smaller = obs.resize(scale=0.5)
    assert smaller.size == (32, 24)
    assert smaller.to_root(0, 0) == (0.0, 0.0)
    assert smaller.to_root(4, 6) == (8.0, 12.0)


def test_resize_by_width_keeps_aspect_ratio():
    obs = _observation(data=_image(64, 48))
    smaller = obs.resize(width=32)
    assert smaller.size == (32, 24)


def test_resize_rejects_conflicting_arguments():
    obs = _observation()
    with pytest.raises(ValueError):
        obs.resize(scale=0.5, width=10)
    with pytest.raises(ValueError):
        obs.resize()


def test_chained_derivations_compose_back_to_root():
    """crop -> resize must fold into one correct mapping."""
    obs = _observation(data=_image(64, 48))
    chained = obs.crop(10, 5, 20, 12).resize(scale=0.5)

    assert chained.size == (10, 6)
    assert chained.to_root(0, 0) == (10.0, 5.0)
    assert chained.to_root(2, 2) == (14.0, 9.0)
    assert [step.kind for step in chained.lineage()] == ["resize", "crop"]
    assert chained.root is obs


def test_derived_payload_stays_read_only():
    obs = _observation()
    assert obs.crop(0, 0, 4, 4).data.flags.writeable is False
    assert obs.resize(scale=0.5).data.flags.writeable is False


# --------------------------------------------------------------------------- #
# PixelTransform
# --------------------------------------------------------------------------- #

def test_pixel_transform_crop_and_resize():
    assert PixelTransform.crop(10, 5).to_parent(0, 0) == (10.0, 5.0)
    assert PixelTransform.resize(0.5, 0.5).to_parent(4, 6) == (8.0, 12.0)


def test_pixel_transform_rejects_zero_scale():
    with pytest.raises(ValueError):
        PixelTransform.resize(0.0, 1.0)


def test_derivation_records_parent_and_kind():
    obs = _observation()
    cropped = obs.crop(2, 3, 5, 5)
    derivation = cropped.derived_from
    assert isinstance(derivation, Derivation)
    assert derivation.parent is obs
    assert derivation.kind == "crop"
