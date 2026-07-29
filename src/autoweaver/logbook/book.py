"""Logbook — one run, one book.

The anchor is a ship's log. A voyage gets **one** book; the officer on watch
writes into it three kinds of entry — the hourly fix (position, speed), the
things that happen (course change, another vessel sighted), and the attachments
folded in behind (charts, photographs). Every entry is timed, and that shared
timeline is what makes the book readable years later: you can put the noon
position next to the entry about the storm because both carry a time from the
same clock.

That maps onto a run without stretching:

===============================  =========================================
ship's log                        here
===============================  =========================================
one voyage, one book              one run, one directory
the hourly fix                    periodic board samples
entries for things that happen    PLC exchanges, decisions
charts folded in behind           captured frames
every entry timed                 every row carries ``t`` and ``wall``
===============================  =========================================

**Two clocks on every row, and both are load-bearing.** ``t`` is monotonic
seconds since the run started — immune to NTP steps and DST, so intervals
computed from it are true. ``wall`` is epoch seconds — meaningless for intervals
but the only thing that lines the run up against anything outside the process
(an operator's note, a PLC's own log, another machine). Recording one and
deriving the other is not possible after the fact, so both go down.

What a Logbook does **not** do: interpret. It never learns what ``func=60``
means or what counts as a successful pick. It owns the directory, the clock, the
identity block and the accounting; the business owns the meaning of every field
it writes. That boundary is the same one ``TrajectoryRecorder`` already draws,
and it is what keeps this reusable.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from autoweaver.logbook.attachment import Attachment, AttachmentWriter
from autoweaver.logbook.identity import (
    config_fingerprint,
    git_sha_dirty,
    resolve_batch,
)
from autoweaver.logbook.root import (
    RUN_STAMP_FORMAT,
    prune_old_runs,
    resolve_root,
)
from autoweaver.logbook.scribe import Scribe
from autoweaver.logbook.serialize import to_jsonable
from autoweaver.sensor.delivery import DropPolicy

logger = logging.getLogger(__name__)


class Logbook:
    """One run's directory, clock, identity and ledgers.

    Construct it directly for full control, or use :meth:`start` for the usual
    case where the identity block is derived from a source tree and a config
    mapping.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        identity: Optional[dict] = None,
        row_tags: Optional[dict] = None,
        context: Optional[dict] = None,
        attachment_capacity: Optional[int] = None,
        attachment_policy: DropPolicy = DropPolicy.DROP_NEWEST,
    ) -> None:
        """
        Args:
            run_dir: This run's own directory. Created if missing.
            identity: Written once to ``meta.json``. Anything the business wants
                on record about *this* run — code version, config hash, serial
                numbers, calibration constants.
            row_tags: Stamped onto **every** row of every ledger. Keep it to a
                couple of small fields (a batch number, a machine id): the point
                is that a single row, pulled out of a pile of runs concatenated
                together, still says where it came from without a join back to
                ``meta.json``. One int and one short string per row is free; a
                whole identity block is not.
            context: Initial value of the **mutable** per-row context — see
                :meth:`set_context`. Where ``row_tags`` is what stays true for
                the whole run, this is what is true *right now*.
            attachment_capacity: Queue depth for large payloads. Required before
                any attachment can be written — there is no sensible framework
                default when one payload is 9.4 MB and the next is 35 MB.
            attachment_policy: What a full attachment queue drops.
        """
        # ``resolve_root`` rather than ``Path.expanduser`` — see its docstring for
        # the directory-literally-named-'~' trap and why the raising variant is
        # the wrong one to use on a start-up path.
        self.run_dir = resolve_root(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Anchor both clocks at the same instant so ``t`` and ``wall`` on a row
        # describe the same moment rather than two reads a few microseconds apart.
        self._t0_mono = time.monotonic()
        self._t0_wall = time.time()

        self._identity = dict(identity or {})
        self._row_tags = dict(row_tags or {})

        # Run-*variable* context, guarded by its own lock: the thread that sets
        # it (the one driving the process) is not the thread that reads it (a
        # background sampler, a delivery thread), so an unguarded dict would tear.
        self._context: dict[str, Any] = dict(context or {})
        self._context_lock = threading.Lock()

        self._lock = threading.Lock()
        self._scribes: dict[str, Scribe] = {}
        self._counts: dict[str, dict[str, int]] = {}
        self._summary: dict[str, Any] = {}
        self._closed = False

        self._attachment_capacity = attachment_capacity
        self._attachment_policy = attachment_policy
        self._attachments: Optional[AttachmentWriter] = None

        self._write_meta()
        logger.info("logbook -> %s", self.run_dir)

    # -- construction ------------------------------------------------------- #

    @classmethod
    def start(
        cls,
        root: str | Path,
        *,
        source_dir: Optional[str | Path] = None,
        config: Any = None,
        machine_id: str = "",
        batch_mode: str = "startup_increment",
        retention_days: float = 0.0,
        extra_identity: Optional[dict] = None,
        stamp: Optional[str] = None,
        context: Optional[dict] = None,
        **kwargs: Any,
    ) -> "Logbook":
        """Open a new run directory under ``root`` with a derived identity block.

        ``root`` holds the per-run directories *and* the artefacts that outlive a
        single run (the batch counter), so the counter survives across runs.
        ``source_dir`` must point at the **code**, not at ``root``: asking git
        about a data directory yields ``"unknown"`` for every run and the mistake
        stays invisible until the field is needed.

        ``retention_days`` sweeps older runs before opening this one; ``0`` — the
        default — keeps everything. Unlike ``root``, a safe default *does* exist
        here, and "delete nothing" is it: filling a disk is visible and
        recoverable, deleting the wrong run is neither. A site that wants the
        sweep asks for it. See :func:`~autoweaver.logbook.root.prune_old_runs`.
        """
        root_path = resolve_root(root)
        root_path.mkdir(parents=True, exist_ok=True)
        # Sweep before opening: the new run's own directory does not exist yet, so
        # it cannot be caught by its own policy however the clock is set.
        prune_old_runs(root_path, retention_days)
        stamp = stamp or datetime.datetime.now().strftime(RUN_STAMP_FORMAT)
        batch, mode = resolve_batch(root_path, mode=batch_mode)

        identity = {
            "run_stamp": stamp,
            "git_sha": git_sha_dirty(source_dir),
            "config_hash": config_fingerprint(config, jsonable=to_jsonable),
            "machine_id": machine_id,
            "batch": batch,
            "batch_mode": mode,
            "t_wall_start": round(time.time(), 4),
        }
        identity.update(extra_identity or {})

        # Only these two ride on every row — see ``row_tags`` in __init__.
        row_tags = {"batch": batch}
        if machine_id:
            row_tags["machine_id"] = machine_id

        # ``<root>/runs/<stamp>``, never ``<root>/<stamp>``: the root also holds
        # what outlives a run (the batch counter, and whatever the business puts
        # beside it). Keeping books in their own subdirectory is what lets the
        # sweep enumerate runs without having to recognise — and spare — every
        # other thing living at the root.
        return cls(
            root_path / "runs" / stamp,
            identity=identity,
            row_tags=row_tags,
            context=context,
            **kwargs,
        )

    #: Keys ``from_config`` recognises — mirrors the ``start`` parameters that a
    #: config file can sensibly carry. The rest of ``start`` (``source_dir``,
    #: ``config``, ``context``, ``extra_identity``) is code-supplied: a YAML file
    #: cannot hand over a live config mapping or the path to its own source tree.
    _CONFIG_KEYS = frozenset({
        "root",
        "retention_days",
        "machine_id",
        "batch_mode",
        "attachment_capacity",
        "attachment_policy",
    })

    @classmethod
    def from_config(cls, config: dict[str, Any], /, **runtime: Any) -> "Logbook":
        """Open a book from a business config mapping, mirroring
        :meth:`TrajectoryRecorder.from_config`.

        The framework does **not** read YAML itself — the business owns its config
        file and hands over the relevant section::

            import yaml
            cfg = yaml.safe_load(open("cell.yaml"))
            book = Logbook.from_config(cfg["logbook"], source_dir=".", config=cfg)

        with, e.g.::

            # cell.yaml
            logbook:
              root: ~/robot-data         # required — no default, see below
              retention_days: 7          # 0 keeps everything
              machine_id: cell-01
              attachment_capacity: 12    # required before any attachment is written
              attachment_policy: drop_newest

        ``root`` is **required and has no framework default**. Any default would be
        a guess about somebody else's disk layout, and the failure mode of guessing
        wrong is the worst kind: the process starts, runs, and writes tens of
        gigabytes somewhere nobody is looking. A missing ``root`` should stop
        start-up, loudly, while a human is still watching.

        Unknown keys raise, so a YAML typo fails at load instead of silently
        recording with a default nobody chose.

        The config section is **positional-only**. ``start`` already has a
        ``config`` parameter meaning something else entirely — the whole business
        config, hashed into the identity block — and a caller needs to pass both:
        ``from_config(cfg["logbook"], config=cfg)``. Taking the section by
        position keeps the framework's ``from_config`` naming without the two
        senses of the word colliding on one call.
        """
        if not isinstance(config, dict):
            raise ValueError(
                f"logbook config must be a mapping, got {type(config).__name__}"
            )
        unknown = set(config) - cls._CONFIG_KEYS
        if unknown:
            raise ValueError(
                f"unknown logbook config key(s): {sorted(unknown)}; "
                f"recognised keys are {sorted(cls._CONFIG_KEYS)}"
            )
        if not config.get("root"):
            raise ValueError(
                "logbook config must specify 'root' — where run data lives is a "
                "deployment decision and the framework will not guess it"
            )

        settings = {k: v for k, v in config.items() if k != "root"}
        policy = settings.get("attachment_policy")
        if isinstance(policy, str):
            try:
                settings["attachment_policy"] = DropPolicy(policy)
            except ValueError:
                raise ValueError(
                    f"unknown attachment_policy {policy!r}; recognised values are "
                    f"{sorted(p.value for p in DropPolicy)}"
                ) from None
        settings.update(runtime)
        return cls.start(config["root"], **settings)

    # -- clock -------------------------------------------------------------- #

    def now(self) -> dict:
        """The pair of timestamps stamped on every row."""
        return {
            "t": round(time.monotonic() - self._t0_mono, 4),
            "wall": round(time.time(), 4),
        }

    def at(self, mono: float) -> dict:
        """Timestamps for a moment that already happened, given its monotonic time.

        For callers that know *when* better than ``now()`` does: a sensor knows
        when the shutter fired, a tick-driven recorder knows the tick's timestamp.
        Stamping the write time instead would smear a whole tick's rows across
        however long the writing took, and would put a frame's row at the time it
        reached disk rather than the time it was taken.
        """
        return {
            "t": round(mono - self._t0_mono, 4),
            "wall": round(self._t0_wall + (mono - self._t0_mono), 4),
        }

    @property
    def row_tags(self) -> dict:
        return dict(self._row_tags)

    @property
    def identity(self) -> dict:
        """A snapshot of what went into ``meta.json``.

        Copied, so a caller cannot reshape this run's recorded identity after
        the fact. Exposed because the business usually needs a field or two back
        out — the batch number to label an export, the config hash to compare
        against a previous run — and re-deriving them (or fishing them out of
        ``row_tags``, which deliberately carries only a subset) means two places
        that can disagree about the same run.
        """
        return dict(self._identity)

    # -- mutable context ---------------------------------------------------- #

    def set_context(self, **pairs: Any) -> None:
        """Replace the per-row context: from now on, rows carry these.

        The run-constant tags say *which run*; this says *where the run had got
        to* when a row was written. Without it a trace is a wall of samples with
        no way to ask "what was happening here" — which is precisely why pluck
        stamps phase/round/attempt on every trajectory sample and could not use
        the framework recorder, which had no way to carry them.

        Keys are the caller's vocabulary. This deliberately does **not** fix them
        as ``phase``/``round``/``attempt``: those are one product's process
        model, and a framework that hard-codes them makes every other project
        translate into a shape that does not fit.

        Replaces rather than merges, so a key that stops being meaningful stops
        appearing instead of lingering at a stale value. Use
        :meth:`update_context` to change part of it.
        """
        with self._context_lock:
            self._context = {str(k): v for k, v in pairs.items()}

    def update_context(self, **pairs: Any) -> None:
        """Merge into the per-row context, leaving the other keys alone."""
        with self._context_lock:
            self._context.update({str(k): v for k, v in pairs.items()})

    @property
    def context(self) -> dict:
        """A snapshot of the current context. Copied, so the caller cannot
        mutate the shared dict from outside the lock."""
        with self._context_lock:
            return dict(self._context)

    # -- ledgers ------------------------------------------------------------ #

    def scribe(self, ledger: str, **defaults: Any) -> "Scribe":
        """A writer bound to one ledger file (``<ledger>.jsonl``).

        Ledgers are separate files on purpose. A PLC exchange happens orders of
        magnitude more often than a pick decision, and interleaving them buries
        the decisions — the rows you actually read — under traffic. One ledger
        per rhythm keeps each one readable on its own.

        Asking twice for the same ledger returns the same scribe, so two workers
        that both record "events" share one file and one sequence, and the file
        is opened exactly once.
        """
        with self._lock:
            existing = self._scribes.get(ledger)
            if existing is not None:
                return existing
            scribe = Scribe(self, ledger, defaults=defaults)
            self._scribes[ledger] = scribe
            return scribe

    # -- attachments -------------------------------------------------------- #

    @property
    def attachments(self) -> AttachmentWriter:
        """The shared attachment writer, created on first use.

        Raises if no capacity was configured: a wrong-by-construction default is
        worse than a loud failure at wiring time.
        """
        with self._lock:
            if self._attachments is None:
                if self._attachment_capacity is None:
                    raise RuntimeError(
                        "logbook: attachment_capacity was not set, so large "
                        "payloads cannot be written. Pass it when opening the "
                        "Logbook — the right depth depends on payload size and "
                        "memory budget, which only the caller knows"
                    )
                self._attachments = AttachmentWriter(
                    capacity=self._attachment_capacity,
                    policy=self._attachment_policy,
                )
            return self._attachments

    def submit_attachment(self, seq: int, attachment: Attachment) -> Optional[str]:
        """Queue one attachment; returns its run-relative path, or ``None`` if dropped.

        The sequence number prefixes the filename so attachments sort in write
        order and two rows asking for ``frame.png`` cannot collide.
        """
        rel_dir = Path(attachment.subdir) if attachment.subdir else Path()
        rel_path = rel_dir / f"{seq:06d}_{attachment.filename}"
        accepted = self.attachments.submit(self.run_dir / rel_path, attachment)
        return rel_path.as_posix() if accepted else None

    # -- accounting --------------------------------------------------------- #

    def count(self, ledger: str, kind: str) -> None:
        """Tally one row. Used for ``summary.json``; cheap enough to do inline."""
        with self._lock:
            per_ledger = self._counts.setdefault(ledger, {})
            per_ledger[kind] = per_ledger.get(kind, 0) + 1

    #: Keys ``_write_summary`` owns. The business cannot use these — see
    #: :meth:`summarise`.
    _RESERVED_SUMMARY_KEYS = frozenset({
        "duration_s", "rows", "kinds", "attachments",
    })

    def summarise(self, **fields: Any) -> None:
        """Contribute fields to ``summary.json``. Merged, last write wins.

        The framework can only count what it can see — rows per ledger, bytes
        that failed to land. "How many hairs came out of this run" is a fact only
        the business holds, and without a way to hand it over the business is
        left reading ``summary.json`` back, merging, and rewriting it: three
        chances to lose the file and a window where it is half-written.

        Call it whenever the number is known (as it changes, or once at the end);
        the file is written at :meth:`close`.

        Raises on a key this class owns (``duration_s``, ``rows``, ``kinds``,
        ``attachments``). Silently letting one side clobber the other would make
        ``summary.json`` mean different things depending on call order, and a
        wrong summary is worse than a missing field.
        """
        clashes = self._RESERVED_SUMMARY_KEYS.intersection(fields)
        if clashes:
            raise ValueError(
                f"summary key(s) {sorted(clashes)} are written by the logbook "
                f"itself; pick another name"
            )
        with self._lock:
            self._summary.update(fields)

    # -- shutdown ----------------------------------------------------------- #

    def close(self, *, attachment_timeout: float = 5.0) -> None:
        """Drain attachments, close ledgers, write ``summary.json``. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            scribes = list(self._scribes.values())
            writer = self._attachments

        drained = True
        if writer is not None:
            drained = writer.close(timeout=attachment_timeout)

        for scribe in scribes:
            scribe.close()

        self._write_summary(writer, drained)

    # -- files -------------------------------------------------------------- #

    def _write_meta(self) -> None:
        if not self._identity:
            return
        try:
            (self.run_dir / "meta.json").write_text(
                json.dumps(to_jsonable(self._identity), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - a run without meta.json still records
            logger.exception("logbook: could not write meta.json")

    def _write_summary(self, writer: Optional[AttachmentWriter], drained: bool) -> None:
        with self._lock:
            counts = {k: dict(v) for k, v in self._counts.items()}
            contributed = dict(self._summary)
        # Business fields first so the framework's own keys cannot be displaced
        # by a late contribution; ``summarise`` already refuses the reserved
        # names, this is the belt to that pair of braces.
        summary: dict[str, Any] = dict(contributed)
        summary.update({
            "duration_s": round(time.monotonic() - self._t0_mono, 3),
            "rows": {ledger: sum(kinds.values()) for ledger, kinds in counts.items()},
            "kinds": counts,
        })
        if writer is not None:
            summary["attachments"] = {
                "written": writer.written,
                # Read the tally rather than take it: close() has already run, so
                # zeroing here would leave nothing for a caller that wants to
                # report the loss itself. Includes anything abandoned at close.
                "dropped": writer.dropped,
                "drained": drained,
                # Named, not just counted: which unit of work lost data is the
                # part you need months later.
                **({"abandoned": [p.name for p in writer.abandoned]}
                   if writer.abandoned else {}),
            }
        try:
            (self.run_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            logger.exception("logbook: could not write summary.json")


__all__ = ["Logbook", "RUN_STAMP_FORMAT"]
