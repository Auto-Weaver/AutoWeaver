"""Scribe — the hand that writes into the logbook.

One verb: :meth:`Scribe.write`. What happens depends on what you hand it, not on
which method you picked. A scribe is bound to **one ledger**, and that binding is
its entire reason to exist as a separate object: the PLC worker's scribe always
writes to ``plc.jsonl``, the vision worker's to ``events.jsonl``, and neither has
to remember which file it belongs to or repeat the fields that are true of every
one of its rows. Several scribes, one book.

A scribe is **passive**. It does not tick, it owns no thread, it is not a Worker.
"Sample the board every 20 ms" is not a feature it has — it is somebody else
calling ``write`` every 20 ms. Keeping the timing outside is what lets the same
object serve a 50 Hz sampler, a once-per-pick decision, and an error that happens
twice a shift.

Three layers land on every row, and they differ by how long they stay true:

* ``row_tags`` — constant for the whole run (batch, machine).
* ``context`` — where the run has **got to** right now, mutable, shared by every
  scribe in the book (see :meth:`Logbook.set_context`).
* ``defaults`` — constant for this scribe, whatever is true of all its rows.

Narrower wins on a collision, and an explicit argument to ``write`` wins over all
three.

The seven rules of ``write``
----------------------------
1. **The time is stamped for you.** Callers never pass a timestamp (they may
   pass a *moment* — see ``at`` — but never a formatted time).
2. **Run identity and current context are stamped for you**, so one row lifted
   out of a pile of runs still says both where it came from and what was
   happening when it was written.
3. **What the fields mean is the caller's business.** This module never learns
   what ``func=60`` is, and must not: the moment a framework starts interpreting
   business fields, every project has to bend its vocabulary to fit.
4. **Big payloads become files**; the row keeps their names.
5. **Writing never blocks and never raises.** A recorder that can take production
   down has inverted its own priority. Rows are a locked append (microseconds);
   attachments go to a bounded queue with its own thread.

   *Where this guarantee actually ends.* Dispatch is by **type**: hand over an
   :class:`Attachment` and it is queued, hand over anything else and it becomes
   part of the row, serialised and flushed on the calling thread. But "expensive"
   is not a type. A dict with ten thousand keys is a row by type and a large
   payload by cost, and it will take the synchronous path — serialising and
   flushing in front of the producer, which is exactly what rule 5 promises not
   to do. No size threshold is imposed here on purpose: there is no real payload
   sitting near such a line today, so any number would be invented rather than
   measured, and a wrong threshold silently reclassifies things. **The honest
   statement is that rule 5 holds for rows of ordinary size, and that "ordinary"
   is currently undefined.** If a caller starts writing rows big enough to matter,
   this is the boundary that has to be drawn — with a measurement behind it.
6. **One ledger per rhythm.** High-frequency traffic in the same file as sparse
   decisions makes the decisions unfindable.
7. **A drop is recorded.** Losing data is survivable; losing it silently is not,
   because the loss is then discovered offline, long after the run that could
   have been repeated.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any, IO, Optional, Sequence

from autoweaver.logbook.attachment import Attachment
from autoweaver.logbook.serialize import to_jsonable

if TYPE_CHECKING:  # pragma: no cover
    from autoweaver.logbook.book import Logbook

logger = logging.getLogger(__name__)


class Scribe:
    """A write-entry bound to one ledger of one :class:`~autoweaver.logbook.book.Logbook`.

    Obtained from :meth:`Logbook.scribe`, not constructed directly.
    """

    def __init__(
        self,
        logbook: "Logbook",
        ledger: str,
        *,
        defaults: Optional[dict] = None,
    ) -> None:
        self._logbook = logbook
        self._ledger = ledger
        self._defaults = dict(defaults or {})
        self._path = logbook.run_dir / f"{ledger}.jsonl"
        self._lock = threading.Lock()
        self._seq = 0
        self._dropped_attachments = 0
        self._file: Optional[IO[str]] = None

    # -- introspection ------------------------------------------------------ #

    @property
    def ledger(self) -> str:
        return self._ledger

    @property
    def path(self):
        return self._path

    @property
    def rows(self) -> int:
        with self._lock:
            return self._seq

    # -- the one verb ------------------------------------------------------- #

    def write(
        self,
        kind: str,
        *,
        at: Optional[float] = None,
        attachments: Optional[Sequence[Attachment]] = None,
        **fields: Any,
    ) -> None:
        """Write one row. **Never blocks on I/O of consequence, never raises.**

        Args:
            kind: What happened, in the caller's vocabulary. Used for the
                per-kind tally in ``summary.json`` and for nothing else — this
                module attaches no meaning to it.
            at: Monotonic time of the moment being recorded, if the caller knows
                it better than "now" (a shutter instant, a tick timestamp). Rows
                written from a background thread minutes after the fact would
                otherwise claim the time they reached disk.
            attachments: Large payloads. Each becomes a file; the row records
                the run-relative paths under ``attachments``, and a dropped one
                is recorded under ``attachments_dropped`` rather than vanishing.
            **fields: Anything else. Coerced to JSON-safe types; unknown types
                degrade to a string rather than costing the row.
        """
        try:
            self._write(kind, at, attachments, fields)
        except Exception:  # noqa: BLE001 - rule 5, and it is the whole rule
            logger.exception(
                "logbook: dropping a '%s' row for ledger '%s' after an error",
                kind, self._ledger,
            )

    def _write(
        self,
        kind: str,
        at: Optional[float],
        attachments: Optional[Sequence[Attachment]],
        fields: dict,
    ) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq

        # ``kind`` is a dict key in the summary tally, so a caller that passes a
        # list or a dict here used to lose the entire row to a TypeError raised
        # *after* the row had been built. Coercing costs nothing and keeps the
        # data; rule 5 is about not costing the caller anything, and losing a row
        # over the shape of one argument is a cost.
        if not isinstance(kind, str):
            kind = str(kind)

        stamp = self._logbook.at(at) if at is not None else self._logbook.now()
        row: dict[str, Any] = {"seq": seq, **stamp, "kind": kind}
        # Widest scope first, narrowest last, so the more specific value wins:
        # run-constant tags, then where-the-run-is-now context, then this
        # scribe's standing fields, then what this call passed.
        row.update(self._logbook.row_tags)
        row.update({k: to_jsonable(v) for k, v in self._logbook.context.items()})
        row.update({k: to_jsonable(v) for k, v in self._defaults.items()})
        row.update({k: to_jsonable(v) for k, v in fields.items()})

        if attachments:
            written: dict[str, str] = {}
            dropped: list[str] = []
            for attachment in attachments:
                rel = self._logbook.submit_attachment(seq, attachment)
                if rel is None:
                    dropped.append(attachment.filename)
                else:
                    written[attachment.filename] = rel
            if written:
                row["attachments"] = written
            if dropped:
                # Rule 7. The row that would have referenced the file says
                # instead that it is missing, so the gap is visible in the same
                # place someone would look for the file.
                row["attachments_dropped"] = dropped
                with self._lock:
                    self._dropped_attachments += len(dropped)

        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            if self._file is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self._path.open("a", encoding="utf-8")
            self._file.write(line)
            # Flushed per row: a run that dies mid-pick is exactly the run whose
            # last few rows matter most, and the OS buffer would eat them. The
            # cost is a write syscall, not an fsync.
            self._file.flush()
        self._logbook.count(self._ledger, kind)

    # -- accounting --------------------------------------------------------- #

    def take_dropped_attachments(self) -> int:
        """Attachments this scribe could not queue, zeroed as it is read.

        Pulled, not pushed: a drop storm is exactly when a per-drop callback on
        the producer's thread is least affordable.
        """
        with self._lock:
            n, self._dropped_attachments = self._dropped_attachments, 0
            return n

    # -- shutdown ----------------------------------------------------------- #

    def close(self) -> None:
        with self._lock:
            if self._file is None:
                return
            try:
                self._file.flush()
                self._file.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.exception("logbook: closing ledger '%s' raised", self._ledger)
            finally:
                self._file = None


__all__ = ["Scribe"]
