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
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

try:  # numpy is a hard dependency in practice, but recording must not need it
    import numpy as np
except Exception:  # pragma: no cover - exercised only on a numpy-less install
    np = None  # type: ignore[assignment]


def to_jsonable(value: Any) -> Any:
    """Best-effort, lossless-where-possible conversion of a value to JSON types.

    Unknown types degrade to ``repr`` rather than raising — a surprising value
    in one field must never cost the whole record.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if np is not None:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):  # np.float64, np.int64, ...
            return value.item()
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return {k: to_jsonable(v) for k, v in asdict(value).items()}
        except Exception:  # noqa: BLE001 - asdict can fail on odd fields
            return repr(value)
    if isinstance(value, (tuple, list)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return repr(value)


__all__ = ["to_jsonable"]
