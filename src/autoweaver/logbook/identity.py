"""Run identity — the answer to "which code, which settings, which machine?"

Every helper here is **best-effort and never fatal**. A run off a loose checkout,
on a box without git, with an unreadable counter file, must still record. The
worst acceptable outcome is a field reading ``"unknown"``; the unacceptable one
is a run that refuses to start because it could not describe itself.

Why this exists at all: months later, "this batch came out better" is only a
usable observation if you can tell whether the code changed, the thresholds
changed, or neither. Without an identity block that question is unanswerable and
the recorded data is worth much less than it cost to collect.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"


def git_sha_dirty(cwd: Optional[str | Path] = None) -> str:
    """Short git SHA of HEAD, with ``-dirty`` appended if the tree has changes.

    ``cwd`` must point at the **source tree**, not at wherever the run data is
    written. The question this answers is "which code produced this run", so it
    has to be asked where the code lives — pointing it at a data directory
    outside any repository stamps every run ``"unknown"`` and the mistake is
    invisible until you need the field.

    Any failure (not a repo, no git binary, timeout) yields ``"unknown"``.
    """
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=5,
        )
        if rev.returncode != 0:
            return UNKNOWN
        sha = rev.stdout.strip() or UNKNOWN
        if sha == UNKNOWN:
            return sha
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=5,
        )
        if status.returncode == 0 and status.stdout.strip():
            sha += "-dirty"
        return sha
    except Exception as exc:  # noqa: BLE001 - identity is never worth a crash
        logger.debug("git_sha_dirty failed: %s", exc)
        return UNKNOWN


def config_fingerprint(config: Any, *, jsonable=None) -> str:
    """Stable short hash of the effective configuration.

    Order-independent (``sort_keys``) so a reordered YAML does not look like a
    different setup, and coerced through ``jsonable`` first so numpy scalars or
    dataclass leaves cannot raise. Twelve hex digits: enough to group runs,
    short enough to eyeball in a filename or a log line.

    ``jsonable`` defaults to the package's own coercion. It used to default to
    the identity function, which made the fingerprint depend on whether the
    caller remembered to pass one: a config carrying a ``Path`` or a numpy
    scalar hashed fine with a coercer and silently became ``"unknown"``
    without — the same settings yielding two different answers, and the failure
    invisible until someone tried to group runs by it.

    Returns ``"unknown"`` if the config cannot be serialised at all.
    """
    if jsonable is None:
        from autoweaver.logbook.serialize import to_jsonable as jsonable
    coerce = jsonable
    try:
        blob = json.dumps(coerce(config), sort_keys=True, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("config_fingerprint failed: %s", exc)
        return UNKNOWN
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def resolve_batch(
    counter_dir: str | Path,
    *,
    mode: str = "startup_increment",
    filename: str = "batch.json",
) -> tuple[int, str]:
    """Resolve this run's batch number off a persistent counter file.

    Two modes:

    * ``startup_increment`` — read, ``+1``, write back. Every process start is a
      new batch. Suits development, where "one run, one batch" is what you want.
    * ``external`` — read and trust, **never write**. This is the seam for a
      plant system (HMI/MES) to own batch assignment: the file *is* the
      interface, so taking it over needs no code change here.

    Best-effort throughout: a missing or corrupt file starts from 0, and an
    unwritable directory means the increment is not persisted (logged, not
    fatal). Returns ``(batch, mode)``.
    """
    path = Path(counter_dir) / filename
    stored = 0
    try:
        if path.exists():
            stored = int(json.loads(path.read_text(encoding="utf-8")).get("batch", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("batch counter %s unreadable (%s) — starting from 0", path, exc)
        stored = 0

    if mode == "external":
        return stored, mode

    batch = stored + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"batch": batch, "mode": mode, "updated": round(time.time(), 3)},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not persist %s (%s) — batch=%d not saved", path, exc, batch)
    return batch, mode


__all__ = ["UNKNOWN", "git_sha_dirty", "config_fingerprint", "resolve_batch"]
