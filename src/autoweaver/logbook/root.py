"""The data root — resolving where books live, and sweeping the old ones.

The root is the directory that holds every run's book plus the few artefacts that
outlive a single run (a batch counter, a "latest" pointer). Two jobs live here
because both are about the root as a whole rather than about any one book.

Neither job is glamorous, and both are the kind of thing every project rewrites
slightly wrong. That is exactly why they belong in the framework: there is no
business judgement in either, only a trap.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: ``<root>/runs/<stamp>`` directory-name format — spelled once, here.
RUN_STAMP_FORMAT = "%Y%m%d_%H%M%S"


def resolve_root(raw: str | Path) -> Path:
    """Expand ``~`` in a configured root and return it as a :class:`Path`.

    This function exists because of one specific, silent, expensive mistake.

    ``yaml.safe_load`` hands back the **literal string** ``"~/robot-data"``. Passing
    that straight to ``Path(...).mkdir(parents=True)`` does not fail and does not
    expand anything: it cheerfully creates a directory *named* ``~`` under the
    current working directory — which, for a process started from the source tree,
    is the source tree. The data then lands in exactly the place that configuring a
    root was meant to move it out of, and nothing complains. It is usually found
    weeks later, by ``git status``.

    ``os.path.expanduser`` is used rather than :meth:`Path.expanduser` on purpose:
    the latter **raises** ``RuntimeError`` when the home directory cannot be
    determined. Start-up must not die over a log path. With ``$HOME`` unset,
    ``os.path.expanduser`` falls back to the ``pwd`` entry, and if even that fails
    it returns the string unchanged — which is the one case worth shouting about,
    because the run would then write into a literal ``~`` folder after all.
    """
    text = str(raw)
    expanded = os.path.expanduser(text)
    if expanded.startswith("~"):
        logger.warning(
            "logbook: could not expand '~' in root %r (no HOME and no pwd entry) — "
            "data would land in a directory literally named '~' under %s. "
            "Configure an absolute path.",
            text, os.getcwd(),
        )
    return Path(expanded)


def parse_run_stamp(name: str) -> Optional[datetime]:
    """``"20260728_143005"`` -> datetime; anything else -> ``None``.

    The round-trip check at the end is not paranoia. ``strptime`` is **lenient about
    field widths**, so a truncated or hand-edited name like ``"20260728_1010"``
    parses happily as 10:01:00 — a half-written directory would be handed a
    plausible age and could then be swept. Re-formatting the parsed value and
    demanding it equal the original name means only exactly-shaped stamps are ever
    candidates for deletion.
    """
    try:
        stamp = datetime.strptime(name, RUN_STAMP_FORMAT)
    except ValueError:
        return None
    return stamp if stamp.strftime(RUN_STAMP_FORMAT) == name else None


def _dir_size_bytes(path: Path) -> int:
    """Total size of files under ``path``; unreadable entries count as zero."""
    total = 0
    for parent, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += (Path(parent) / name).stat().st_size
            except OSError:
                pass
    return total


def prune_old_runs(
    root: str | Path,
    retention_days: float,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Delete ``<root>/runs/<stamp>`` directories older than ``retention_days``.

    Unattended operation is the whole reason this exists: on a factory machine
    nothing ever comes along to tidy up, and a single run is substantial — pluck
    measures roughly **335 MB** each. Call it once at start-up.

    **The default is to keep everything, and that is not an oversight.** Compare
    with ``root``, which is required precisely because no default could be right:
    here a safe default does exist. "Delete nothing" cannot destroy data. The two
    failure modes are not symmetric —

    - default to keeping: worst case the disk fills. Visible, and recoverable.
    - default to sweeping: worst case something irreplaceable is gone. Not
      recoverable, and discovered long after the fact.

    So a site that wants the sweep asks for it. **The framework must never be the
    thing that destroys somebody's data on its own initiative.**

    Three deliberate properties:

    - **Age comes from the directory NAME**, never from file mtime. Copying the tree
      to a USB stick or rsyncing it rewrites mtimes wholesale; a backup must not be
      able to resurrect or condemn a run.
    - **A name that does not parse is never deleted.** Anything a human or a future
      tool dropped in ``runs/`` stays put. Leaving junk is cheap; deleting somebody's
      data is not.
    - **``retention_days <= 0`` disables the sweep entirely** — the escape hatch, and
      the default.

    Every deletion is logged **with the space it freed**, because an operator has to
    be able to tell "the policy removed it" from "the data went missing" — those look
    identical from the outside and lead to very different next steps. Returns a
    summary dict; failures are collected rather than raised, since a run must start
    even if the sweep cannot.
    """
    summary: dict[str, Any] = {
        "deleted": [], "freed_bytes": 0, "kept": 0, "skipped": [], "failed": [],
    }
    if retention_days is None or float(retention_days) <= 0:
        logger.info(
            "logbook: run retention disabled (retention_days=%s) — keeping everything",
            retention_days,
        )
        return summary

    runs_dir = Path(root) / "runs"
    if not runs_dir.is_dir():
        return summary

    cutoff = (now or datetime.now()) - timedelta(days=float(retention_days))
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        stamp = parse_run_stamp(entry.name)
        if stamp is None:
            summary["skipped"].append(entry.name)
            continue
        if stamp >= cutoff:
            summary["kept"] += 1
            continue
        freed = _dir_size_bytes(entry)
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            summary["failed"].append(entry.name)
            logger.warning("logbook: could not remove old run %s: %s", entry, exc)
            continue
        summary["deleted"].append(entry.name)
        summary["freed_bytes"] += freed
        logger.info(
            "logbook: removed run %s (older than %s days, freed %.1f MB)",
            entry.name, retention_days, freed / 1e6,
        )
    return summary


__all__ = [
    "RUN_STAMP_FORMAT",
    "parse_run_stamp",
    "prune_old_runs",
    "resolve_root",
]
