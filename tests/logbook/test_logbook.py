"""Logbook + Scribe — one test per rule of ``write``, plus the mutable context.

Threading here is synchronised with Events and Barriers, never with sleeps: a
sleep-based assertion on a queue is a coin flip that usually lands green and
fails on the one CI run nobody wants to debug.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from autoweaver.logbook import Attachment, Logbook, Scribe
from autoweaver.sensor.delivery import DropPolicy


# ─── helpers ────────────────────────────────────────────────────────────────


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _book(tmp_path: Path, **kwargs) -> Logbook:
    return Logbook(tmp_path / "run", **kwargs)


# ─── rule 1: the time is stamped for you ────────────────────────────────────


def test_every_row_carries_both_clocks(tmp_path):
    book = _book(tmp_path)
    scribe = book.scribe("events")
    scribe.write("hello")
    scribe.write("again")
    book.close()

    rows = _rows(scribe.path)
    assert len(rows) == 2
    for row in rows:
        assert "t" in row and "wall" in row
        assert row["t"] >= 0.0
        # wall is epoch seconds — sanity-check the magnitude rather than the value
        assert row["wall"] > 1_600_000_000
    assert rows[1]["t"] >= rows[0]["t"]


def test_at_records_the_moment_not_the_write_time(tmp_path):
    """A row written late about something that happened earlier must claim the
    earlier time — otherwise a background writer smears the timeline."""
    book = _book(tmp_path)
    scribe = book.scribe("events")
    import time

    earlier = time.monotonic() - 5.0
    scribe.write("late_report", at=earlier)
    now_row_t = book.now()["t"]
    book.close()

    row = _rows(scribe.path)[0]
    # ``t`` is measured from the book's own t0, so a moment 5s before the book
    # opened lands ~5s in the negative. That it can go negative at all is the
    # point: the row claims when the thing happened, not when it was written.
    assert row["t"] == pytest.approx(-5.0, abs=0.5)
    assert row["t"] < now_row_t
    # ``wall`` must be walked back by the same amount rather than left at "now",
    # or the two clocks on one row would disagree about which moment it is.
    assert row["wall"] == pytest.approx(book._t0_wall - 5.0, abs=0.5)


# ─── rule 2: identity and context are stamped for you ───────────────────────


def test_row_tags_land_on_every_row(tmp_path):
    book = _book(tmp_path, row_tags={"batch": 7, "machine_id": "M02"})
    scribe = book.scribe("events")
    scribe.write("a")
    book.close()

    row = _rows(scribe.path)[0]
    assert row["batch"] == 7
    assert row["machine_id"] == "M02"


def test_identity_is_written_once_to_meta(tmp_path):
    book = _book(tmp_path, identity={"git_sha": "abc123", "config_hash": "deadbeef"})
    book.scribe("events").write("a")
    book.close()

    meta = json.loads((book.run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["git_sha"] == "abc123"
    # Identity is NOT repeated on rows — that is what row_tags is for.
    assert "git_sha" not in _rows(book.run_dir / "events.jsonl")[0]


def test_start_survives_an_unusable_source_dir(tmp_path):
    """Identity is best-effort: a run off a non-repo must still record."""
    book = Logbook.start(
        tmp_path / "runs",
        source_dir=tmp_path / "not-a-repo",
        config={"a": 1},
        machine_id="M01",
    )
    book.scribe("events").write("a")
    book.close()

    meta = json.loads((book.run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["git_sha"] == "unknown"
    assert meta["config_hash"] != "unknown"
    assert meta["batch"] == 1


# ─── the mutable context ────────────────────────────────────────────────────


def test_context_applies_from_the_moment_it_is_set(tmp_path):
    book = _book(tmp_path)
    scribe = book.scribe("events")

    book.set_context(phase="approach", attempt=1)
    scribe.write("before")
    book.set_context(phase="grasp", attempt=2)
    scribe.write("after")
    book.close()

    before, after = _rows(scribe.path)
    assert (before["phase"], before["attempt"]) == ("approach", 1)
    assert (after["phase"], after["attempt"]) == ("grasp", 2)


def test_set_context_replaces_and_update_context_merges(tmp_path):
    book = _book(tmp_path)
    scribe = book.scribe("events")

    book.set_context(phase="a", attempt=1)
    book.set_context(phase="b")           # replaces: attempt is gone, not stale
    scribe.write("replaced")
    book.update_context(attempt=9)        # merges: phase survives
    scribe.write("merged")
    book.close()

    replaced, merged = _rows(scribe.path)
    assert replaced["phase"] == "b" and "attempt" not in replaced
    assert merged["phase"] == "b" and merged["attempt"] == 9


def test_context_is_shared_by_every_scribe_in_the_book(tmp_path):
    """pluck's evidence: one context, read by the event writer *and* the
    background sampler. Per-scribe contexts would let them disagree."""
    book = _book(tmp_path)
    events, plc = book.scribe("events"), book.scribe("plc")
    book.set_context(phase="lift")
    events.write("decision")
    plc.write("exchange")
    book.close()

    assert _rows(events.path)[0]["phase"] == "lift"
    assert _rows(plc.path)[0]["phase"] == "lift"


def test_context_under_concurrent_writers_never_tears(tmp_path):
    """The setter thread and the writing threads are different threads — pluck
    hit exactly this (BT sets, the sampler reads). Every row must show a pair
    that was set together, never half of one and half of the next."""
    book = _book(tmp_path)
    scribe = book.scribe("events")
    start = threading.Barrier(3)
    rounds = 200

    def setter():
        start.wait()
        for i in range(rounds):
            book.set_context(phase=f"p{i}", attempt=i)

    def writer():
        start.wait()
        # A fixed count, not "until the setter says stop": with a stop flag the
        # setter can finish all its rounds before the writer's first loop check,
        # leaving zero rows and a test that fails for a reason that has nothing
        # to do with tearing.
        for _ in range(rounds):
            scribe.write("sample")

    threads = [threading.Thread(target=setter), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    start.wait()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "a worker thread hung"
    book.close()

    rows = _rows(scribe.path)
    assert len(rows) == rounds
    for row in rows:
        if "phase" in row:
            # phase pN and attempt N were set in the same call; a torn read
            # would pair pN with M != N.
            assert row["phase"] == f"p{row['attempt']}"


# ─── rule 3: the framework does not interpret ───────────────────────────────


@dataclass
class _Pose:
    x: float
    y: float


def test_fields_pass_through_without_interpretation(tmp_path):
    book = _book(tmp_path)
    scribe = book.scribe("plc")
    scribe.write(
        "exchange",
        func=60,                       # framework has no idea what 60 means
        waited_ms=1234,
        block="D",
        values=[1, 2, 3],
    )
    book.close()

    row = _rows(scribe.path)[0]
    assert row["func"] == 60 and row["waited_ms"] == 1234
    assert row["values"] == [1, 2, 3]
    assert row["kind"] == "exchange"


def test_awkward_values_are_coerced_not_rejected(tmp_path):
    book = _book(tmp_path)
    scribe = book.scribe("events")
    scribe.write(
        "sample",
        arr=np.array([[1.0, 2.0], [3.0, 4.0]]),
        scalar=np.float64(2.5),
        pose=_Pose(1.0, 2.0),
        weird=object(),
    )
    book.close()

    row = _rows(scribe.path)[0]
    assert row["arr"] == [[1.0, 2.0], [3.0, 4.0]]   # lossless
    assert row["scalar"] == 2.5
    assert row["pose"] == {"x": 1.0, "y": 2.0}
    assert row["weird"].startswith("<object object")  # degraded, not fatal


# ─── rule 4: big payloads become files ──────────────────────────────────────


def test_attachment_becomes_a_file_and_the_row_keeps_its_name(tmp_path):
    book = _book(tmp_path, attachment_capacity=4)
    scribe = book.scribe("events")
    scribe.write(
        "recognize",
        attachments=[
            Attachment("frame.png", lambda p: p.write_bytes(b"PNGDATA"), subdir="frames")
        ],
        hairs=3,
    )
    book.close()

    row = _rows(scribe.path)[0]
    rel = row["attachments"]["frame.png"]
    assert rel.startswith("frames/")
    assert (book.run_dir / rel).read_bytes() == b"PNGDATA"
    assert row["hairs"] == 3


def test_attachment_needs_a_configured_capacity(tmp_path):
    """No framework default: 9.4 MB and 35 MB frames on one rig make any
    guessed number wrong for someone."""
    book = _book(tmp_path)  # no attachment_capacity
    with pytest.raises(RuntimeError, match="attachment_capacity"):
        book.attachments


# ─── rule 5: never blocks, never raises ─────────────────────────────────────


def test_a_wedged_attachment_writer_does_not_block_the_caller(tmp_path):
    book = _book(tmp_path, attachment_capacity=1, attachment_policy=DropPolicy.DROP_NEWEST)
    scribe = book.scribe("events")
    wedged = threading.Event()
    entered = threading.Event()

    def slow(path: Path) -> None:
        entered.set()
        wedged.wait(10)
        path.write_bytes(b"x")

    scribe.write("first", attachments=[Attachment("a.bin", slow)])
    assert entered.wait(5), "the writer thread never picked the job up"

    # Producer keeps going at full speed while the writer is stuck.
    for i in range(50):
        scribe.write(f"row{i}", attachments=[Attachment("b.bin", slow)])

    assert scribe.rows == 51        # nothing blocked
    wedged.set()
    book.close()
    assert scribe.take_dropped_attachments() > 0


def test_a_raising_field_never_kills_the_row_write(tmp_path):
    class Exploding:
        def __repr__(self):
            raise RuntimeError("boom")

    book = _book(tmp_path)
    scribe = book.scribe("events")
    scribe.write("ok_before")
    scribe.write("boom", bad=Exploding())   # must not raise
    scribe.write("ok_after")
    book.close()

    kinds = [r["kind"] for r in _rows(scribe.path)]
    assert "ok_before" in kinds and "ok_after" in kinds


def test_a_failing_attachment_write_does_not_stop_recording(tmp_path):
    book = _book(tmp_path, attachment_capacity=4)
    scribe = book.scribe("events")

    def explode(path: Path) -> None:
        raise OSError("disk on fire")

    scribe.write("bad", attachments=[Attachment("x.bin", explode)])
    scribe.write("good")
    book.close()

    assert [r["kind"] for r in _rows(scribe.path)] == ["bad", "good"]


# ─── rule 6: one ledger per rhythm ──────────────────────────────────────────


def test_ledgers_are_separate_files(tmp_path):
    book = _book(tmp_path)
    events, plc = book.scribe("events"), book.scribe("plc")
    events.write("decision")
    for _ in range(20):
        plc.write("poll")
    book.close()

    assert len(_rows(events.path)) == 1     # not buried under 20 PLC rows
    assert len(_rows(plc.path)) == 20
    assert events.path != plc.path


def test_asking_twice_for_a_ledger_returns_the_same_scribe(tmp_path):
    book = _book(tmp_path)
    a = book.scribe("events")
    b = book.scribe("events")
    assert a is b
    a.write("one")
    b.write("two")
    book.close()
    assert [r["seq"] for r in _rows(a.path)] == [1, 2]   # one sequence, one file


def test_scribe_defaults_ride_on_its_rows_only(tmp_path):
    book = _book(tmp_path)
    plc = book.scribe("plc", peer="plc1")
    events = book.scribe("events")
    plc.write("exchange")
    events.write("decision")
    book.close()

    assert _rows(plc.path)[0]["peer"] == "plc1"
    assert "peer" not in _rows(events.path)[0]


def test_narrower_scope_wins_on_a_collision(tmp_path):
    book = _book(tmp_path, row_tags={"who": "run"})
    book.set_context(who="context")
    scribe = book.scribe("events", who="scribe")
    scribe.write("a")
    scribe.write("b", who="call")
    book.close()

    rows = _rows(scribe.path)
    assert rows[0]["who"] == "scribe"
    assert rows[1]["who"] == "call"


# ─── rule 7: a drop is recorded ─────────────────────────────────────────────


def test_dropped_attachments_are_recorded_on_the_row_and_in_the_tally(tmp_path):
    book = _book(tmp_path, attachment_capacity=1, attachment_policy=DropPolicy.DROP_NEWEST)
    scribe = book.scribe("events")
    wedged = threading.Event()
    entered = threading.Event()

    def slow(path: Path) -> None:
        entered.set()
        wedged.wait(10)

    scribe.write("first", attachments=[Attachment("a.bin", slow)])
    assert entered.wait(5)
    # Queue capacity 1 and the only slot is occupied -> these get refused.
    for i in range(5):
        scribe.write(f"later{i}", attachments=[Attachment("b.bin", slow)])

    rows = _rows(scribe.path)
    refused = [r for r in rows if "attachments_dropped" in r]
    assert refused, "a drop left no trace on the row — the worst outcome"
    assert refused[0]["attachments_dropped"] == ["b.bin"]

    tally = scribe.take_dropped_attachments()
    assert tally == len(refused)
    assert scribe.take_dropped_attachments() == 0    # zeroed as it is read

    wedged.set()
    book.close()


def test_summary_reports_counts_and_attachment_losses(tmp_path):
    book = _book(tmp_path, attachment_capacity=4)
    events = book.scribe("events")
    plc = book.scribe("plc")
    events.write("decision")
    events.write("decision")
    plc.write("exchange")
    events.write("recognize", attachments=[Attachment("f.bin", lambda p: p.write_bytes(b"z"))])
    book.close()

    summary = json.loads((book.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["rows"] == {"events": 3, "plc": 1}
    assert summary["kinds"]["events"]["decision"] == 2
    assert summary["attachments"]["written"] == 1
    assert summary["attachments"]["dropped"] == 0
    assert summary["duration_s"] >= 0


# ─── lifecycle ──────────────────────────────────────────────────────────────


def test_close_is_idempotent(tmp_path):
    book = _book(tmp_path)
    book.scribe("events").write("a")
    book.close()
    book.close()
    assert (book.run_dir / "summary.json").exists()


# ─── opening from config ────────────────────────────────────────────────────


def test_from_config_opens_a_book(tmp_path):
    book = Logbook.from_config({"root": str(tmp_path), "machine_id": "cell-01"})
    assert book.run_dir.parent == tmp_path / "runs"
    book.close()


def test_root_is_required_and_has_no_default(tmp_path):
    """No default is the point. A guessed root lets the process start, run, and
    write tens of gigabytes somewhere nobody is looking; a missing one should
    stop start-up while a human is still watching."""
    with pytest.raises(ValueError, match="root"):
        Logbook.from_config({"machine_id": "cell-01"})
    with pytest.raises(ValueError, match="root"):
        Logbook.from_config({"root": ""})


def test_an_unknown_key_fails_loudly(tmp_path):
    """A YAML typo must fail at load, not record silently with a default."""
    with pytest.raises(ValueError, match="retenshun_days"):
        Logbook.from_config({"root": str(tmp_path), "retenshun_days": 7})


def test_config_must_be_a_mapping():
    with pytest.raises(ValueError, match="mapping"):
        Logbook.from_config(["root", "/data"])  # type: ignore[arg-type]


def test_attachment_policy_is_accepted_as_a_string(tmp_path):
    """YAML has no enums; the string in the file has to land as the policy."""
    book = Logbook.from_config({
        "root": str(tmp_path),
        "attachment_capacity": 4,
        "attachment_policy": "latest_only",
    })
    assert book._attachment_policy is DropPolicy.LATEST_ONLY
    book.close()


def test_an_unknown_policy_name_names_the_valid_ones(tmp_path):
    with pytest.raises(ValueError, match="drop_newest"):
        Logbook.from_config({
            "root": str(tmp_path),
            "attachment_capacity": 4,
            "attachment_policy": "drop_middle",
        })


def test_runtime_arguments_reach_start(tmp_path):
    """``source_dir``/``config`` cannot come from YAML — a config file cannot
    hand over a live mapping or the path to its own source tree."""
    book = Logbook.from_config(
        {"root": str(tmp_path)},
        config={"exposure": 12000},
        context={"phase": "boot"},
    )
    meta = json.loads((book.run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["config_hash"]
    scribe = book.scribe("events")
    scribe.write("hello")
    book.close()
    assert _rows(scribe.path)[0]["phase"] == "boot"


def test_books_live_under_runs_so_the_root_can_hold_other_things(tmp_path):
    """``<root>/runs/<stamp>``, never ``<root>/<stamp>``.

    The root also holds what outlives a single run (the batch counter). Books get
    their own subdirectory so the sweep can enumerate runs without having to
    recognise — and spare — everything else living at the root.
    """
    book = Logbook.start(tmp_path, machine_id="M01")
    assert book.run_dir.is_dir()
    assert book.run_dir.parent == (tmp_path / "runs")
    assert book.run_dir.parent.parent == tmp_path
    book.close()


def test_scribe_is_not_a_worker(tmp_path):
    """It must stay passive: no tick, no thread, nothing the clock drives."""
    book = _book(tmp_path)
    scribe = book.scribe("events")
    assert not hasattr(scribe, "on_tick")
    assert not hasattr(scribe, "on_attach")
    assert isinstance(scribe, Scribe)
    book.close()
