"""Logbook — recording what happened, for whoever reads it later.

Named for a ship's log: one voyage, one book, every entry timed off the same
clock. A :class:`Logbook` owns a run's directory, its clock and its identity; a
:class:`Scribe` is a hand bound to one ledger in that book and writes rows into
it; an :class:`Attachment` is a payload too big to sit inside a row, so the row
keeps its filename and the bytes go to a file beside it.

**Recording is downstream of the kernel, not part of it.** Everything here is a
passive consumer: it reads what the system already publishes — board state,
observations, whatever the business hands over — and persists it. It writes no
control state, sends no notes, and takes part in no decision. Keep it that way:
the moment recording can influence control, turning logging off stops being a
free action.

It also never interprets. This package will not learn what ``func=60`` means or
what a successful pick is. It owns *when* a row is written, *where* it lands, and
*what happens when it cannot keep up*; the business owns what the fields mean.
"""

from autoweaver.logbook.attachment import Attachment, AttachmentWriter
from autoweaver.logbook.book import Logbook
from autoweaver.logbook.identity import (
    config_fingerprint,
    git_sha_dirty,
    resolve_batch,
)
from autoweaver.logbook.root import (
    RUN_STAMP_FORMAT,
    expand_user_path,
    parse_run_stamp,
    prune_old_runs,
    resolve_root,
)
from autoweaver.logbook.scribe import Scribe
from autoweaver.logbook.serialize import to_jsonable
from autoweaver.logbook.trajectory import TrajectoryRecorder

__all__ = [
    "Attachment",
    "AttachmentWriter",
    "Logbook",
    "RUN_STAMP_FORMAT",
    "Scribe",
    "TrajectoryRecorder",
    "config_fingerprint",
    "expand_user_path",
    "git_sha_dirty",
    "parse_run_stamp",
    "prune_old_runs",
    "resolve_batch",
    "resolve_root",
    "to_jsonable",
]
