"""Attachments — the payloads too big to live inside a line of the ledger.

A frame is 9.4 MB on one of pluck's cameras and 35 MB on the other. Neither goes
into a JSONL row, so the row carries a **filename** and the bytes go to a file
beside it. That is rule 4 of ``write``.

Rule 5 is why this module has a thread. Encoding one of those frames to PNG was
measured at ~247 ms against a ~31 ms capture — the writer is 8x slower than the
producer, so doing it on the caller's thread hands the recorder a veto over
production speed. It gets its own thread and a **bounded** queue instead, reusing
:class:`~autoweaver.sensor.delivery.ObservationQueue` rather than growing a
second copy of the drop policy: the subtle part (which item to lose when full,
and the fact that losing it is counted) is identical, and two copies of it would
drift.

The framework never encodes anything. It cannot: PNG encoding is OpenCV, which
is a business dependency and a business decision (format, compression level,
colour space). The caller hands over a callable that knows how to put bytes at a
path; this module decides *when* it runs, *where* the file goes, and what happens
when it cannot keep up.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from autoweaver.sensor.delivery import DropPolicy, ObservationQueue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Attachment:
    """One large payload to be written beside a ledger row.

    Attributes:
        filename: Name the file should get, extension included
            (``"frame.png"``). The logbook prefixes it with the row's sequence
            number so files sort in write order and cannot collide.
        write: ``write(path)`` — puts the bytes there. Runs on the attachment
            thread, so it may be slow; it must not touch anything the caller
            keeps mutating. Exceptions are caught and logged, never propagated
            into the producer.
        subdir: Optional folder under the run directory. Use it to keep a
            high-volume stream out of the sparse decision frames — otherwise the
            interesting handful becomes unfindable among thousands.
    """

    filename: str
    write: Callable[[Path], None]
    subdir: str = ""


@dataclass(frozen=True)
class _Job:
    path: Path
    attachment: Attachment


class AttachmentWriter:
    """One thread + one bounded queue, writing attachments to disk.

    ``submit`` never blocks and never raises. It returns ``False`` when the queue
    was full and the attachment was dropped, which is the caller's only chance to
    notice — and the reason the answer is synchronous rather than a callback.
    """

    def __init__(
        self,
        *,
        capacity: int,
        policy: DropPolicy = DropPolicy.DROP_NEWEST,
        name: str = "logbook-attachments",
    ) -> None:
        """``capacity`` is required. See ``delivery``'s module docstring for why
        no framework default is possible: the right depth depends on payload
        size, service time and memory budget, all of which the caller knows and
        this module does not.

        ``DROP_NEWEST`` is the default because it is the safe one for archival:
        the earliest items of a burst are usually the reference the later ones
        are compared against, so when something must be lost it should be the
        most recent, not the baseline.
        """
        self._queue: ObservationQueue[_Job] = ObservationQueue(
            capacity=capacity, policy=policy
        )
        self._name = name
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._written = 0
        self._abandoned: list[Path] = []

    # -- producer side ------------------------------------------------------ #

    def submit(self, path: Path, attachment: Attachment) -> bool:
        """Queue one attachment. ``False`` means it was dropped, not written."""
        accepted = self._queue.offer(_Job(path=path, attachment=attachment))
        if accepted:
            self._ensure_thread()
        return accepted

    def take_dropped(self) -> int:
        """Drop tally, zeroed as it is read.

        Includes anything abandoned by :meth:`close`, so calling this *after*
        closing is what collects the full loss for a run.
        """
        return self._queue.take_dropped()

    @property
    def dropped(self) -> int:
        return self._queue.dropped

    @property
    def written(self) -> int:
        return self._written

    @property
    def depth(self) -> int:
        """Attachments queued but not yet on disk.

        The backlog is how you tell "the writer is keeping up" from "the writer
        is falling behind but has not started losing anything **yet**". The drop
        tally only moves once the queue is already full, which is too late to be
        a warning — by then the loss has happened. A burst that ends with a
        non-trivial depth is the signal that capacity or write speed needs
        attention before the next one overflows.
        """
        return self._queue.depth

    # -- writer thread ------------------------------------------------------ #

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name=self._name
            )
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._queue.take(timeout=0.2)
            if job is None:
                if self._queue.closed:
                    return
                continue
            self._idle.clear()
            try:
                job.path.parent.mkdir(parents=True, exist_ok=True)
                job.attachment.write(job.path)
                self._written += 1
            except Exception:  # noqa: BLE001 - a bad write must not end recording
                logger.exception(
                    "logbook: attachment write failed for %s", job.path
                )
            finally:
                if self._queue.depth == 0:
                    self._idle.set()

    # -- shutdown ----------------------------------------------------------- #

    def drain(self, timeout: float) -> bool:
        """Wait for the backlog to hit disk. ``False`` means it timed out."""
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if self._queue.depth == 0 and self._idle.is_set():
                return True
            if self._thread is None or not self._thread.is_alive():
                return self._queue.depth == 0
            self._idle.wait(0.02)
        return self._queue.depth == 0 and self._idle.is_set()

    @property
    def abandoned(self) -> tuple[Path, ...]:
        """Paths of attachments dropped at :meth:`close`, in queue order.

        Kept — rather than only counted — because a number is not actionable.
        Attachments are usually filed per unit of work (``lift_move/pt3_poke1/``),
        so "3 lost" leaves you unable to say *whose* data is now incomplete,
        while the paths say it directly. Empty until ``close`` has run.
        """
        with self._lock:
            return tuple(self._abandoned)

    def close(self, timeout: float = 5.0) -> bool:
        """Drain, then stop. Anything still queued after ``timeout`` is abandoned.

        Abandoning rather than waiting forever is deliberate: this runs during
        teardown, where an unbounded wait turns Ctrl+C into a hang.

        Whatever was abandoned **is charged to the drop tally** (so
        :meth:`take_dropped` after close reports the whole run's loss) and its
        paths are kept on :attr:`abandoned` (so the loss can be attributed, not
        merely counted). Rule 7 has no shutdown exemption: a frame lost here is
        exactly as gone as one refused at the door, and the shutdown path must
        not be the one place where data disappears quietly.
        """
        drained = self.drain(timeout)
        self._stop.set()
        self._queue.close()
        # Charge and record the leftovers before joining, so the tally is
        # complete by the time close() returns and a caller can report it.
        lost = self._queue.abandon()
        if lost:
            with self._lock:
                self._abandoned.extend(job.path for job in lost)
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        if lost:
            logger.warning(
                "logbook: abandoned %d unwritten attachment(s) at close: %s",
                len(lost),
                ", ".join(str(job.path) for job in lost[:5])
                + (" ..." if len(lost) > 5 else ""),
            )
        return drained


__all__ = ["Attachment", "AttachmentWriter"]
