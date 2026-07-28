"""The data root — expansion, and the sweep.

Both jobs here are ones every project rewrites slightly wrong, so the tests are
written against the *traps* rather than against the happy path.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import pytest

from autoweaver.logbook.root import (
    RUN_STAMP_FORMAT,
    parse_run_stamp,
    prune_old_runs,
    resolve_root,
)


# ─── resolving the root ─────────────────────────────────────────────────────


def test_tilde_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_root("~/robot-data") == tmp_path / "robot-data"


def test_an_unexpanded_tilde_would_make_a_directory_called_tilde(monkeypatch, tmp_path):
    """The trap this function exists for, stated as a test.

    ``Path("~/x").mkdir(parents=True)`` does not fail and does not expand: it
    creates a directory *named* ``~`` under the CWD. Data then lands next to the
    source instead of where the root said, and nothing complains.
    """
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    Path("~/naive").mkdir(parents=True)
    assert (tmp_path / "~" / "naive").is_dir(), "precondition: this is the trap"

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_root("~/naive") == tmp_path / "home" / "naive"


def test_expansion_failure_warns_loudly_but_does_not_raise(monkeypatch, caplog):
    """No HOME and no pwd entry. ``Path.expanduser`` raises here; start-up must
    not die over a log path, so this returns the string and shouts instead."""
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p)

    with caplog.at_level(logging.WARNING):
        out = resolve_root("~/robot-data")

    assert str(out) == "~/robot-data"
    assert any("could not expand" in r.message.lower() or "~" in r.message
               for r in caplog.records)


def test_an_absolute_root_is_left_alone(tmp_path):
    assert resolve_root(tmp_path / "data") == tmp_path / "data"


# ─── parsing a run stamp ────────────────────────────────────────────────────


def test_a_well_formed_stamp_parses():
    assert parse_run_stamp("20260728_143005") == datetime(2026, 7, 28, 14, 30, 5)


def test_strptime_leniency_does_not_smuggle_a_truncated_name_through():
    """``strptime`` is lenient about field widths: ``"20260728_1010"`` parses
    happily as 10:01:00. Without the round-trip check a half-written directory
    would be handed a plausible age — and could then be swept."""
    assert datetime.strptime("20260728_1010", RUN_STAMP_FORMAT)  # it really does parse
    assert parse_run_stamp("20260728_1010") is None


@pytest.mark.parametrize("name", ["", "notes", "run_1", "20260728", "latest.png"])
def test_anything_else_is_not_a_stamp(name):
    assert parse_run_stamp(name) is None


# ─── sweeping ───────────────────────────────────────────────────────────────


def _make_run(root, when: datetime, *, size: int = 0):
    d = root / "runs" / when.strftime(RUN_STAMP_FORMAT)
    d.mkdir(parents=True)
    if size:
        (d / "blob.bin").write_bytes(b"x" * size)
    return d


def test_old_runs_go_and_recent_ones_stay(tmp_path):
    now = datetime(2026, 7, 28, 12, 0, 0)
    old = _make_run(tmp_path, now - timedelta(days=30), size=1000)
    recent = _make_run(tmp_path, now - timedelta(hours=2))

    summary = prune_old_runs(tmp_path, retention_days=7, now=now)

    assert not old.exists()
    assert recent.is_dir()
    assert summary["deleted"] == [old.name]
    assert summary["kept"] == 1
    assert summary["freed_bytes"] >= 1000


def test_a_name_that_does_not_parse_is_never_deleted(tmp_path):
    """Leaving junk is cheap; deleting somebody's data is not."""
    now = datetime(2026, 7, 28, 12, 0, 0)
    _make_run(tmp_path, now - timedelta(days=30))
    stranger = tmp_path / "runs" / "hand-copied-run"
    stranger.mkdir(parents=True)

    summary = prune_old_runs(tmp_path, retention_days=7, now=now)

    assert stranger.is_dir()
    assert summary["skipped"] == ["hand-copied-run"]


def test_age_comes_from_the_name_not_the_mtime(tmp_path):
    """An rsync or a USB copy rewrites mtimes wholesale. A backup must not be
    able to resurrect — or condemn — a run."""
    now = datetime(2026, 7, 28, 12, 0, 0)
    old = _make_run(tmp_path, now - timedelta(days=30))
    os.utime(old, (now.timestamp(), now.timestamp()))  # freshly "touched"

    prune_old_runs(tmp_path, retention_days=7, now=now)

    assert not old.exists(), "swept on its name, despite a brand-new mtime"


@pytest.mark.parametrize("retention", [0, 0.0, -1])
def test_retention_of_zero_or_less_disables_the_sweep(tmp_path, retention):
    now = datetime(2026, 7, 28, 12, 0, 0)
    ancient = _make_run(tmp_path, now - timedelta(days=999))

    summary = prune_old_runs(tmp_path, retention_days=retention, now=now)

    assert ancient.is_dir()
    assert summary["deleted"] == []


def test_a_missing_runs_directory_is_not_an_error(tmp_path):
    assert prune_old_runs(tmp_path, retention_days=7)["deleted"] == []


def test_files_at_the_root_are_left_alone(tmp_path):
    """The root holds cross-run artefacts. The sweep only looks inside ``runs/``."""
    now = datetime(2026, 7, 28, 12, 0, 0)
    _make_run(tmp_path, now - timedelta(days=30))
    counter = tmp_path / "batch.json"
    counter.write_text("{}")

    prune_old_runs(tmp_path, retention_days=7, now=now)

    assert counter.exists()
