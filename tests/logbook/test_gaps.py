"""What pluck hit as the first real user of the logbook layer.

Every case here comes from a specific thing that went wrong (or could not be
expressed) when the layer was first wired into a live system, rather than from
imagining how it might be used. Grouped by the gap they close so the reason
survives longer than the memory of the bug.
"""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path

import pytest

from autoweaver.logbook.attachment import Attachment, AttachmentWriter
from autoweaver.logbook.book import Logbook
from autoweaver.logbook.identity import config_fingerprint
from autoweaver.logbook.serialize import to_jsonable
from autoweaver.sensor.delivery import DropPolicy, ObservationQueue


def _blocking_attachment(released: threading.Event, filename: str = "f.png"):
    """An attachment whose write parks until ``released`` is set."""

    def write(path: Path) -> None:
        released.wait(timeout=5.0)
        path.write_bytes(b"x")

    return Attachment(filename=filename, write=write)


# --- gap 1: abandoned-at-close must reach the tally ------------------------- #
#
# The docstring promised "whatever was abandoned still shows up in the tally";
# the code only logged a warning. Losing data at shutdown was the one place the
# accounting had a hole, which is exactly where it is least likely to be noticed.


def test_attachments_abandoned_at_close_are_counted(tmp_path):
    released = threading.Event()
    writer = AttachmentWriter(capacity=4, policy=DropPolicy.DROP_NEWEST)

    started = threading.Event()

    def blocker(path: Path) -> None:
        started.set()
        released.wait(timeout=5.0)
        path.write_bytes(b"x")

    # First one occupies the thread; the rest queue up behind it.
    writer.submit(tmp_path / "a.png", Attachment(filename="a.png", write=blocker))
    assert started.wait(timeout=5.0)
    for name in ("b.png", "c.png", "d.png"):
        writer.submit(
            tmp_path / name,
            Attachment(filename=name, write=lambda p: p.write_bytes(b"x")),
        )

    # Close without letting the blocker finish: the queued three are abandoned.
    writer.close(timeout=0.05)
    released.set()

    assert writer.dropped == 3, "abandoned attachments must be on the tally"
    assert writer.take_dropped() == 3, "and reachable through take_dropped()"


def test_abandoned_attachments_can_be_attributed(tmp_path):
    """Gap 2 — a count says how much was lost, not whose data is incomplete."""
    released = threading.Event()
    writer = AttachmentWriter(capacity=4, policy=DropPolicy.DROP_NEWEST)
    started = threading.Event()

    def blocker(path: Path) -> None:
        started.set()
        released.wait(timeout=5.0)
        path.write_bytes(b"x")

    writer.submit(tmp_path / "poke1" / "a.png",
                  Attachment(filename="a.png", write=blocker))
    assert started.wait(timeout=5.0)
    writer.submit(tmp_path / "poke3" / "b.png",
                  Attachment(filename="b.png", write=lambda p: p.write_bytes(b"")))

    writer.close(timeout=0.05)
    released.set()

    abandoned = writer.abandoned
    assert len(abandoned) == 1
    # The path carries the unit of work, which is the whole point of keeping it.
    assert abandoned[0].parent.name == "poke3"


def test_observations_abandoned_at_close_are_counted():
    """Same hole, same shape, in the delivery queue the attachment writer reuses."""
    q: ObservationQueue[int] = ObservationQueue(
        capacity=4, policy=DropPolicy.DROP_NEWEST
    )
    for i in range(3):
        assert q.offer(i)
    assert q.dropped == 0

    lost = q.abandon()

    assert lost == [0, 1, 2], "abandon returns what it discarded"
    assert q.dropped == 3, "and charges it to the tally"
    assert q.depth == 0


def test_abandon_on_an_empty_queue_is_a_no_op():
    q: ObservationQueue[int] = ObservationQueue(
        capacity=2, policy=DropPolicy.DROP_NEWEST
    )
    assert q.abandon() == []
    assert q.dropped == 0


# --- gap 3: backlog is visible before it becomes loss ----------------------- #


def test_attachment_depth_is_public(tmp_path):
    released = threading.Event()
    writer = AttachmentWriter(capacity=8, policy=DropPolicy.DROP_NEWEST)
    started = threading.Event()

    def blocker(path: Path) -> None:
        started.set()
        released.wait(timeout=5.0)

    writer.submit(tmp_path / "a.png", Attachment(filename="a.png", write=blocker))
    assert started.wait(timeout=5.0)
    writer.submit(tmp_path / "b.png",
                  Attachment(filename="b.png", write=lambda p: p.write_bytes(b"")))

    # Falling behind, but nothing lost yet — the state the drop tally cannot show.
    assert writer.depth == 1
    assert writer.dropped == 0

    released.set()
    writer.close(timeout=2.0)


# --- gap 4: identity is readable back -------------------------------------- #


def test_logbook_exposes_identity(tmp_path):
    book = Logbook(tmp_path / "run", identity={"batch": 7, "config_hash": "abc"})
    try:
        assert book.identity["batch"] == 7
        assert book.identity["config_hash"] == "abc"
    finally:
        book.close()


def test_identity_is_a_copy(tmp_path):
    book = Logbook(tmp_path / "run", identity={"batch": 7})
    try:
        book.identity["batch"] = 999
        assert book.identity["batch"] == 7, "the recorded identity is not editable"
    finally:
        book.close()


# --- gap 5: the business can put its own numbers in summary.json ----------- #


def test_business_fields_land_in_summary(tmp_path):
    book = Logbook(tmp_path / "run")
    book.summarise(hairs_plucked=12, attempts=15)
    book.summarise(hairs_plucked=13)  # last write wins
    book.close()

    summary = json.loads((book.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["hairs_plucked"] == 13
    assert summary["attempts"] == 15
    # ...alongside, not instead of, what the framework counts.
    assert "duration_s" in summary and "rows" in summary


def test_summarise_refuses_framework_keys(tmp_path):
    book = Logbook(tmp_path / "run")
    try:
        for reserved in ("duration_s", "rows", "kinds", "attachments"):
            with pytest.raises(ValueError, match=reserved):
                book.summarise(**{reserved: 1})
    finally:
        book.close()


# --- gap 6: ordinary types must survive as values, not as repr ------------- #


@pytest.mark.parametrize(
    "value, expected",
    [
        (Path("/data/run/x.png"), "/data/run/x.png"),
        (datetime.datetime(2026, 7, 29, 14, 30, 5), "2026-07-29T14:30:05"),
        (datetime.date(2026, 7, 29), "2026-07-29"),
        (datetime.timedelta(seconds=1.5), 1.5),
    ],
)
def test_standard_types_keep_their_value(value, expected):
    assert to_jsonable(value) == expected


def test_paths_nested_in_structures_are_converted():
    out = to_jsonable({"frame": Path("a/b.png"), "more": [Path("c.png")]})
    assert out == {"frame": "a/b.png", "more": ["c.png"]}


def test_bytes_do_not_bloat_a_row():
    assert to_jsonable(b"\x00" * 1024) == "<1024 bytes>"


def test_a_path_written_to_a_ledger_is_usable(tmp_path):
    """The end-to-end version: a repr string here is valid JSON and useless."""
    book = Logbook(tmp_path / "run")
    book.scribe("events").write("saved", where=Path("frames/0001.png"))
    book.close()

    row = json.loads(
        (book.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert row["where"] == "frames/0001.png"


def test_config_fingerprint_defaults_to_the_package_coercion():
    """Gap 6b — the answer must not depend on remembering to pass a coercer."""
    config = {"save_dir": Path("~/data"), "exposure": 12000}

    assert config_fingerprint(config) == config_fingerprint(
        config, jsonable=to_jsonable
    )
    assert config_fingerprint(config) != "unknown"


# --- audit: promises that were not kept ------------------------------------ #
#
# Not from pluck — found by walking every "never raises" / "must not cost the
# row" claim in logbook/ and sensor/ and trying to break it. All three cost a
# whole row of data, which is the outcome rule 5 exists to prevent.


class _Unreprable:
    def __repr__(self):
        raise RuntimeError("boom")


def test_to_jsonable_survives_a_raising_repr():
    """serialize.py says "Nothing here may raise" — repr is user code."""
    assert isinstance(to_jsonable(_Unreprable()), str)


def test_to_jsonable_survives_a_self_referential_structure():
    d: dict = {}
    d["self"] = d
    lst: list = []
    lst.append(lst)

    json.dumps(to_jsonable(d))     # RecursionError before the depth cap
    json.dumps(to_jsonable(lst))


def test_an_odd_field_does_not_cost_the_row(tmp_path):
    """The docstring promises a bad field degrades; it used to lose the row."""
    book = Logbook(tmp_path / "run")
    scribe = book.scribe("events")
    scribe.write("decision", good=1, bad=_Unreprable())
    book.close()

    lines = (book.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "the row must survive one unserialisable field"
    row = json.loads(lines[0])
    assert row["good"] == 1
    assert isinstance(row["bad"], str)


def test_a_non_string_kind_does_not_cost_the_row(tmp_path):
    """``kind`` is a tally key; an unhashable one used to raise after the row
    had already been built."""
    book = Logbook(tmp_path / "run")
    book.scribe("events").write(["odd", "kind"])
    book.close()

    lines = (book.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "['odd', 'kind']"
