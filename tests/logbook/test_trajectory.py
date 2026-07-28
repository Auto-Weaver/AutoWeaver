from __future__ import annotations

import json
import time

import numpy as np
import pytest

from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.logbook.trajectory import (
    SCHEMA_ID,
    TrajectoryRecorder,
    _to_jsonable,
)
from autoweaver.worker.clock import BTClock


# ─── helpers ───────────────────────────────────────────────────────────────


def _pose(x: float) -> np.ndarray:
    """A 4x4 homogeneous pose with translation x along the first axis."""
    m = np.eye(4, dtype=np.float64)
    m[0, 3] = x
    return m


def _declare_arm(board: WorldBoard, ns: str = "arm") -> None:
    board.declare_state(f"{ns}.pose", np.ndarray, writer=ns)
    board.declare_state(f"{ns}.joints", tuple, writer=ns)
    board.declare_state(f"{ns}.busy", bool, writer=ns)


def _post_arm(board: WorldBoard, x: float, busy: bool, ns: str = "arm") -> None:
    board.post_state(f"{ns}.pose", _pose(x), writer=ns)
    board.post_state(f"{ns}.joints", (x,) * 6, writer=ns)
    board.post_state(f"{ns}.busy", busy, writer=ns)


def _read_file(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])["_meta"]
    data = [json.loads(ln) for ln in lines[1:]]
    return meta, data


# ─── _to_jsonable ───────────────────────────────────────────────────────────


def test_to_jsonable_roundtrips_pose_losslessly():
    m = _pose(3.5)
    out = _to_jsonable(m)
    assert out == m.tolist()
    assert np.array_equal(np.array(out), m)


def test_to_jsonable_handles_scalars_tuples_and_unknown():
    assert _to_jsonable(np.float64(1.25)) == 1.25
    assert _to_jsonable((1.0, 2.0, 3.0)) == [1.0, 2.0, 3.0]
    assert _to_jsonable(True) is True
    assert _to_jsonable(None) is None
    # Unknown type never crashes the recorder — it degrades to repr.
    assert _to_jsonable(object()).startswith("<object object")


# ─── recording ──────────────────────────────────────────────────────────────


def test_records_one_line_per_tick_with_meta_header(tmp_path):
    board = WorldBoard()
    clock = BTClock(world_board=board, hz=50)
    _declare_arm(board)

    rec = TrajectoryRecorder("arm", out_dir=tmp_path, name="rec", flush_every=1)
    clock.attach_worker(rec)  # on_start opens the file + writes the meta header

    for i in range(3):
        _post_arm(board, float(i), busy=(i == 1))
        clock.tick_once()

    clock.shutdown()  # detaches -> on_stop closes the file

    files = sorted(tmp_path.glob("rec-*.jsonl"))
    assert len(files) == 1
    meta, data = _read_file(files[0])

    assert meta["schema"] == SCHEMA_ID
    assert meta["tracks"] == ["arm"]
    assert set(meta["track_keys"]["arm"]) == {"pose", "joints", "busy"}

    assert [d["tick"] for d in data] == [0, 1, 2]
    assert all(d["ns"] == "arm" for d in data)
    # raw, convention-free state: pose is a nested 4x4, joints a list.
    assert data[2]["state"]["pose"][0][3] == 2.0
    assert data[0]["state"]["joints"] == [0.0] * 6
    assert data[1]["state"]["busy"] is True
    # both clocks present; wall-clock is real wall time, monotonic increases.
    assert abs(data[0]["t_wall"] - time.time()) < 60
    assert data[2]["t_mono"] >= data[0]["t_mono"]


def test_recorder_publishes_own_progress_state(tmp_path):
    board = WorldBoard()
    clock = BTClock(world_board=board, hz=50)
    _declare_arm(board)
    rec = TrajectoryRecorder("arm", out_dir=tmp_path, name="rec", flush_every=1)
    clock.attach_worker(rec)
    for i in range(4):
        _post_arm(board, float(i), busy=False)
        clock.tick_once()

    assert board.read_state("rec.samples") == 4
    assert board.read_state("rec.path").endswith(".jsonl")
    assert board.read_state("rec.parts") == 1
    clock.shutdown()


def test_only_on_change_skips_identical_state(tmp_path):
    board = WorldBoard()
    clock = BTClock(world_board=board, hz=50)
    _declare_arm(board)
    rec = TrajectoryRecorder(
        "arm", out_dir=tmp_path, name="rec", only_on_change=True, flush_every=1
    )
    clock.attach_worker(rec)

    # Three ticks at the same pose, then one that moves.
    for _ in range(3):
        _post_arm(board, 5.0, busy=False)
        clock.tick_once()
    _post_arm(board, 6.0, busy=False)
    clock.tick_once()

    clock.shutdown()
    _meta, data = _read_file(sorted(tmp_path.glob("rec-*.jsonl"))[0])
    assert [d["state"]["pose"][0][3] for d in data] == [5.0, 6.0]


def test_decimate_records_every_nth_tick(tmp_path):
    board = WorldBoard()
    clock = BTClock(world_board=board, hz=50)
    _declare_arm(board)
    rec = TrajectoryRecorder(
        "arm", out_dir=tmp_path, name="rec", decimate=2, flush_every=1
    )
    clock.attach_worker(rec)
    for i in range(5):  # tick_ids 0..4
        _post_arm(board, float(i), busy=False)
        clock.tick_once()
    clock.shutdown()

    _meta, data = _read_file(sorted(tmp_path.glob("rec-*.jsonl"))[0])
    assert [d["tick"] for d in data] == [0, 2, 4]


def test_multi_track_records_both_namespaces(tmp_path):
    board = WorldBoard()
    clock = BTClock(world_board=board, hz=50)
    _declare_arm(board, "a")
    _declare_arm(board, "b")
    rec = TrajectoryRecorder(["a", "b"], out_dir=tmp_path, name="rec", flush_every=1)
    clock.attach_worker(rec)
    for i in range(2):
        _post_arm(board, float(i), busy=False, ns="a")
        _post_arm(board, float(i) + 100, busy=False, ns="b")
        clock.tick_once()
    clock.shutdown()

    _meta, data = _read_file(sorted(tmp_path.glob("rec-*.jsonl"))[0])
    # 2 ticks x 2 tracks = 4 data lines.
    assert len(data) == 4
    by_ns = {(d["ns"], d["tick"]): d for d in data}
    assert by_ns[("a", 0)]["state"]["pose"][0][3] == 0.0
    assert by_ns[("b", 1)]["state"]["pose"][0][3] == 101.0


def test_out_dir_is_resolved_to_absolute(tmp_path):
    rec = TrajectoryRecorder("arm", out_dir="trajectories", name="rec")
    assert rec._out_dir.is_absolute()


def test_from_config_builds_recorder(tmp_path):
    cfg = {
        "tracks": ["a", "b"],
        "out_dir": str(tmp_path),
        "decimate": 3,
        "only_on_change": True,
    }
    rec = TrajectoryRecorder.from_config(cfg)
    assert rec._tracks == ["a", "b"]
    assert rec._out_dir == tmp_path.resolve()
    assert rec._decimate == 3
    assert rec._only_on_change is True


def test_from_config_requires_tracks():
    with pytest.raises(ValueError, match="must specify 'tracks'"):
        TrajectoryRecorder.from_config({"out_dir": "/tmp/x"})


def test_from_config_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown trajectory config key"):
        TrajectoryRecorder.from_config({"tracks": ["a"], "outdir": "/typo"})


def test_rolls_to_new_file_when_max_bytes_exceeded(tmp_path):
    board = WorldBoard()
    clock = BTClock(world_board=board, hz=50)
    _declare_arm(board)
    # Tiny cap forces a roll every few lines; each part must still be a
    # valid, self-describing file (meta header first).
    rec = TrajectoryRecorder(
        "arm", out_dir=tmp_path, name="rec", max_bytes=400, flush_every=1
    )
    clock.attach_worker(rec)
    for i in range(20):
        _post_arm(board, float(i), busy=False)
        clock.tick_once()
    clock.shutdown()

    files = sorted(tmp_path.glob("rec-*.jsonl"))
    assert len(files) >= 2
    total = 0
    for f in files:
        meta, data = _read_file(f)
        assert meta["schema"] == SCHEMA_ID
        total += len(data)
    assert total == 20  # nothing dropped across the roll boundary
