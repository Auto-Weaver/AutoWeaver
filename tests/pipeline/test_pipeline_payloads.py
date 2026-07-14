"""Tests for the payload-agnostic pipeline layer (0.11.0).

Covers:
- ``PipelineContext`` / ``ProcessStep`` generics with backward-compatible
  untyped usage
- the ``BoxLike`` structural contract consumed by the postprocess steps
- ``RegionDetection`` (bbox-local mask convenience payload)
- the ``FilterStep`` ``classes`` bug regression
- ``YOLOSegStep`` emitting ``SegmentDetection`` into ``ctx.detections``
- ``MaskApplyStep`` reconstructing a full-frame mask from a bbox-local one
"""

import numpy as np
import pytest

from autoweaver.pipeline import (
    BoundingBox,
    Detection,
    PipelineContext,
    PipelineResult,
    ProcessStep,
    RegionDetection,
)
from autoweaver.pipeline.steps.mask_apply import MaskApplyStep
from autoweaver.pipeline.steps.postprocess import BoxLike, FilterStep, NMSStep, SortStep
from autoweaver.pipeline.steps.yolo_seg import SegmentDetection, YOLOSegStep


# ---------------------------------------------------------------------------
# Generics / backward compatibility
# ---------------------------------------------------------------------------


def test_untyped_context_still_works():
    """PipelineContext() with no type argument keeps the old behaviour."""
    ctx = PipelineContext()
    assert ctx.detections == []
    assert ctx.metadata == {}
    ctx.detections.append("anything")  # payload-agnostic container
    assert ctx.detections == ["anything"]


def test_context_is_subscriptable():
    """PipelineContext[D]() constructs and carries a typed payload list."""
    ctx = PipelineContext[Detection]()
    d = Detection(bbox=BoundingBox(0, 0, 1, 1), object_type="x", confidence=0.5)
    ctx.detections.append(d)
    assert ctx.detections == [d]


def test_untyped_step_subclass_still_works():
    """class MyStep(ProcessStep) — no type argument — remains valid."""

    class TagStep(ProcessStep):
        @property
        def name(self):
            return "tag"

        def process(self, ctx):
            ctx.metadata["tagged"] = True
            return ctx

    ctx = PipelineContext()
    out = TagStep().process(ctx)
    assert out is ctx
    assert ctx.metadata["tagged"] is True


def test_result_generic_and_by_type():
    d1 = Detection(bbox=BoundingBox(0, 0, 1, 1), object_type="a", confidence=0.5)
    d2 = Detection(bbox=BoundingBox(0, 0, 1, 1), object_type="b", confidence=0.5)
    res = PipelineResult(detections=[d1, d2], processing_time_ms=1.0, metadata={})
    assert res.detection_count == 2
    assert res.get_detections_by_type("a") == [d1]


# ---------------------------------------------------------------------------
# BoxLike structural contract
# ---------------------------------------------------------------------------


def test_detection_satisfies_boxlike():
    d = Detection(bbox=BoundingBox(0, 0, 1, 1), object_type="x", confidence=0.5)
    assert isinstance(d, BoxLike)


def test_region_detection_satisfies_boxlike():
    rd = RegionDetection(
        bbox=BoundingBox(0, 0, 3, 2),
        object_type="hair",
        confidence=0.9,
        mask=np.ones((3, 4), np.uint8) * 255,
        area_px=12,
    )
    assert isinstance(rd, BoxLike)


def test_arbitrary_object_is_not_boxlike():
    assert not isinstance(object(), BoxLike)

    class Bare:
        bbox = None  # missing object_type / confidence

    assert not isinstance(Bare(), BoxLike)


# ---------------------------------------------------------------------------
# RegionDetection convenience payload
# ---------------------------------------------------------------------------


def test_region_detection_is_detection_and_kw_only():
    rd = RegionDetection(
        bbox=BoundingBox(0, 0, 3, 2),
        object_type="hair",
        confidence=0.9,
        mask=np.ones((2, 3), np.uint8) * 255,
        area_px=6,
    )
    assert isinstance(rd, Detection)
    assert rd.area_px == 6
    assert rd.mask.shape == (2, 3)
    # positional construction of mask/area_px must be rejected (kw_only)
    with pytest.raises(TypeError):
        RegionDetection(BoundingBox(0, 0, 1, 1), "x", 0.5, None, np.zeros((1, 1)), 0)


def test_region_detection_to_dict_excludes_raw_mask():
    rd = RegionDetection(
        bbox=BoundingBox(0, 0, 3, 2),
        object_type="hair",
        confidence=0.9,
        mask=np.ones((2, 3), np.uint8) * 255,
        area_px=6,
    )
    d = rd.to_dict()
    assert d["object_type"] == "hair"
    assert d["mask_shape"] == [2, 3]
    assert d["area_px"] == 6
    assert "mask" not in d


# ---------------------------------------------------------------------------
# Postprocess steps over BoxLike
# ---------------------------------------------------------------------------


def _det(x1, y1, x2, y2, object_type="a", confidence=0.5):
    return Detection(
        bbox=BoundingBox(x1, y1, x2, y2),
        object_type=object_type,
        confidence=confidence,
    )


def test_filter_step_classes_regression():
    """Regression: FilterStep used det.object_type.value (enum), which
    raised AttributeError on the str object_type whenever `classes` was set."""
    ctx = PipelineContext()
    ctx.detections = [
        _det(0, 0, 10, 10, object_type="keep"),
        _det(0, 0, 10, 10, object_type="drop"),
    ]
    step = FilterStep({"classes": ["keep"]})
    out = step.process(ctx)  # must not raise
    assert [d.object_type for d in out.detections] == ["keep"]


def test_filter_step_confidence_and_area():
    ctx = PipelineContext()
    ctx.detections = [
        _det(0, 0, 10, 10, confidence=0.9),  # area 100
        _det(0, 0, 2, 2, confidence=0.9),  # area 4, too small
        _det(0, 0, 10, 10, confidence=0.1),  # low conf
    ]
    step = FilterStep({"min_confidence": 0.5, "min_area": 10})
    out = step.process(ctx)
    assert len(out.detections) == 1


def test_nms_step_suppresses_overlap():
    ctx = PipelineContext()
    ctx.detections = [
        _det(0, 0, 10, 10, confidence=0.9),
        _det(0, 0, 10, 10, confidence=0.5),  # identical box -> suppressed
        _det(100, 100, 110, 110, confidence=0.8),  # far away -> kept
    ]
    step = NMSStep({"iou_threshold": 0.4})
    out = step.process(ctx)
    assert len(out.detections) == 2


def test_sort_step_by_confidence():
    ctx = PipelineContext()
    ctx.detections = [
        _det(0, 0, 1, 1, confidence=0.3),
        _det(0, 0, 1, 1, confidence=0.9),
        _det(0, 0, 1, 1, confidence=0.6),
    ]
    out = SortStep({"by": "confidence"}).process(ctx)
    confs = [d.confidence for d in out.detections]
    assert confs == sorted(confs, reverse=True)


# ---------------------------------------------------------------------------
# YOLOSegStep — emits SegmentDetection into ctx.detections (model mocked)
# ---------------------------------------------------------------------------


class _FakeTensor:
    def __init__(self, value):
        self._value = value

    def __getitem__(self, idx):
        return _FakeTensor(self._value[idx])

    def cpu(self):
        return self

    def numpy(self):
        return self._value


class _FakeBox:
    def __init__(self, conf, cls):
        self.conf = _FakeTensor(np.array([conf]))
        self.cls = _FakeTensor(np.array([cls]))


class _FakeBoxes:
    def __init__(self, confs, clss):
        self._boxes = [_FakeBox(c, k) for c, k in zip(confs, clss)]

    def __len__(self):
        return len(self._boxes)

    def __getitem__(self, i):
        return self._boxes[i]


class _FakeMasks:
    def __init__(self, masks):
        # masks.data[i].cpu().numpy() -> 2D float array
        self.data = [_FakeTensor(m) for m in masks]


class _FakeResult:
    def __init__(self, masks, confs, clss):
        self.masks = _FakeMasks(masks)
        self.boxes = _FakeBoxes(confs, clss)


class _FakeModel:
    names = {0: "hair", 1: "fiber"}

    def __init__(self, results):
        self._results = results

    def predict(self, *args, **kwargs):
        return self._results


def test_yolo_seg_emits_region_detections(monkeypatch):
    # Two model-resolution masks; the step resizes them to the image size.
    img = np.zeros((20, 20, 3), np.uint8)
    m1 = np.zeros((20, 20), np.float32)
    m1[2:8, 3:9] = 1.0  # bbox roughly (3,2)-(8,7)
    m2 = np.zeros((20, 20), np.float32)
    m2[10:15, 11:16] = 1.0

    step = YOLOSegStep({"model": "unused.pt"})
    step._model = _FakeModel([_FakeResult([m1, m2], [0.9, 0.7], [0, 1])])

    ctx = PipelineContext[SegmentDetection]()
    ctx.original_image = img
    ctx.processed_image = img

    out = step.process(ctx)

    # Went into ctx.detections, not a side channel
    assert len(out.detections) == 2
    assert all(isinstance(d, SegmentDetection) for d in out.detections)
    # Sorted by confidence descending
    assert [d.confidence for d in out.detections] == [0.9, 0.7]
    top = out.detections[0]
    assert top.object_type == "hair"
    assert top.class_id == 0
    # Mask is bbox-local, not full frame
    bb = top.bbox
    exp_h = int(bb.y2) - int(bb.y1) + 1
    exp_w = int(bb.x2) - int(bb.x1) + 1
    assert top.mask.shape == (exp_h, exp_w)
    assert top.mask.shape != img.shape[:2]
    assert top.area_px == int(np.count_nonzero(top.mask))
    # ctx.detections is the only output channel — no side-channel alias.
    assert out.metadata["segment_count"] == 2
    assert "segments" not in out.metadata


# ---------------------------------------------------------------------------
# MaskApplyStep — reconstructs full-frame mask from bbox-local mask
# ---------------------------------------------------------------------------


def test_mask_apply_with_bbox_local_mask():
    img = np.full((20, 20, 3), 200, np.uint8)
    # Region occupies rows 5..9, cols 6..11 (inclusive) -> bbox-local 5x6.
    local = np.ones((5, 6), np.uint8) * 255
    seg = SegmentDetection(
        bbox=BoundingBox(6, 5, 11, 9),
        object_type="hair",
        confidence=0.8,
        mask=local,
        area_px=int(np.count_nonzero(local)),
        class_id=0,
    )

    ctx = PipelineContext[SegmentDetection]()
    ctx.original_image = img
    ctx.processed_image = img
    ctx.detections = [seg]

    out = MaskApplyStep().process(ctx)

    # Cropped to the bbox window (inclusive) -> 5 rows x 6 cols
    assert out.processed_image.shape[:2] == (5, 6)
    meta = out.metadata["mask_apply"]
    assert meta["selected_class"] == "hair"
    assert meta["mask_area"] == 30
    # All in-mask pixels retained (original 200), nothing filled to 0
    assert (out.processed_image == 200).all()
