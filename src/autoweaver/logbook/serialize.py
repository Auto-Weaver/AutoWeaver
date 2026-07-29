"""Coercing arbitrary in-process values into something JSON can hold.

Shared by every writer in this package, because they all face the same problem:
the business hands over whatever it happens to have — a numpy pose matrix, a
dataclass, a tuple of floats — and a recorder that raises on an unexpected type
has failed at its one job. **Nothing here may raise.** The worst outcome is a
field that reads as a string instead of a structure; the unacceptable one is a
run that dies because it tried to describe itself.

Conversions are lossless where the type allows: a 4x4 numpy pose round-trips
exactly through nested lists, numpy scalars become Python scalars. There is
deliberately **no** unit conversion, no pose convention, no flattening —
interpretation belongs to the layer that knows what the numbers mean.

**The repr fallback is for genuinely unknown types, not for ordinary ones.**
The difference matters more than it looks: ``repr`` produces valid JSON, so a
type that falls through lands in the file as a string like
``"PosixPath('/data/x.png')"`` — no exception, no warning, and a field that no
downstream tool can use. Standard types that any project will hand over
(``Path``, ``datetime``) are converted explicitly for that reason. When adding a
type here, the test is not "does it crash" but "is the recorded value still the
value".
"""

from __future__ import annotations

import datetime as _datetime
import os
from dataclasses import asdict, is_dataclass
from pathlib import PurePath
from typing import Any

try:  # numpy is a hard dependency in practice, but recording must not need it
    import numpy as np
except Exception:  # pragma: no cover - exercised only on a numpy-less install
    np = None  # type: ignore[assignment]


#: How deep a nested structure is followed before it is summarised instead.
#: A self-referential dict is not a hypothetical: a config object that points
#: back at its parent, or a cached graph node, both produce one, and following
#: it costs a ``RecursionError`` — thrown from inside the "nothing here may
#: raise" module, which is the worst place for one.
_MAX_DEPTH = 24


def to_jsonable(value: Any, _depth: int = 0) -> Any:
    """Best-effort, lossless-where-possible conversion of a value to JSON types.

    Unknown types degrade to ``repr`` rather than raising — a surprising value
    in one field must never cost the whole record.

    **Genuinely total.** Every escape hatch is guarded, including ``repr``
    itself (an object whose ``__repr__`` raises used to take the row down with
    it) and depth (a self-referential structure used to raise ``RecursionError``
    from the one module that promises not to raise). Both failed the same way:
    the caller lost a whole row of data because one field was odd.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _depth >= _MAX_DEPTH:
        return _safe_repr(value)
    # Paths and times before the repr fallback. Both are standard types that
    # turn up in any project touching a filesystem or a schedule, and both have
    # a repr that is *valid JSON and useless data*: a row saying
    # ``"PosixPath('/data/x.png')"`` cannot be fed back to anything, and nothing
    # raises to tell you. Degrading to repr is the right answer for a genuinely
    # surprising type; for these two it is silent corruption of an ordinary one.
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, _datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, (bytes, bytearray)):
        # Length, not content: bytes in a ledger row are almost always a payload
        # that should have been an attachment, and inlining them would bloat the
        # line beyond usefulness.
        return f"<{len(value)} bytes>"
    if np is not None:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):  # np.float64, np.int64, ...
            return value.item()
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return {
                k: to_jsonable(v, _depth + 1) for k, v in asdict(value).items()
            }
        except Exception:  # noqa: BLE001 - asdict can fail on odd fields
            return _safe_repr(value)
    if isinstance(value, (tuple, list)):
        return [to_jsonable(v, _depth + 1) for v in value]
    if isinstance(value, dict):
        try:
            return {
                str(k): to_jsonable(v, _depth + 1) for k, v in value.items()
            }
        except Exception:  # noqa: BLE001 - a key whose __str__ raises
            return _safe_repr(value)
    return _safe_repr(value)


def _safe_repr(value: Any) -> str:
    """``repr`` that cannot raise.

    The last line of defence has to hold: ``__repr__`` is user code, and one
    that raises would otherwise propagate out of a module whose whole contract
    is that it does not. Falls back to the type name, which is still more
    informative than losing the row it was part of.
    """
    try:
        return repr(value)
    except Exception:  # noqa: BLE001
        try:
            return f"<unreprable {type(value).__name__}>"
        except Exception:  # noqa: BLE001 - a broken __class__, seen in proxies
            return "<unreprable>"


__all__ = ["to_jsonable"]
