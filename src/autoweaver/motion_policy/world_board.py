from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Snapshot:
    seq: int
    ts: float
    data: dict[str, Any]
    changed_key: str | None = None
    writer: str | None = None

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.data


@dataclass
class _KeyMeta:
    value_type: type
    writer: str


class WorldBoard:
    """Process-wide observable state with immutable snapshots and rolling history.

    Concurrency model: device threads write, BT main thread reads.
    Writes replace the snapshot ref under a small lock. Reads return the
    current immutable Snapshot — no lock needed because the ref read itself
    is atomic under the GIL.

    History is a sliding window of past snapshot refs (default 100). Each
    write produces a new ref; old refs survive until evicted. Used for
    debugging, replay, and Event Sourcing Query (success rate, recent
    activity, time-windowed analysis).
    """

    DEFAULT_HISTORY_SIZE = 100

    def __init__(self, history_size: int = DEFAULT_HISTORY_SIZE):
        self._meta: dict[str, _KeyMeta] = {}
        self._lock = threading.Lock()
        self._seq = 0
        empty = Snapshot(seq=0, ts=time.monotonic(), data={})
        self._current: Snapshot = empty
        self._history: deque[Snapshot] = deque(maxlen=history_size)
        self._history.append(empty)

    # --- registration ---

    def register(self, key: str, value_type: type, writer: str) -> None:
        with self._lock:
            existing = self._meta.get(key)
            if existing is not None and existing.writer != writer:
                raise ValueError(
                    f"Key '{key}' already owned by '{existing.writer}', "
                    f"cannot assign to '{writer}'"
                )
            self._meta[key] = _KeyMeta(value_type=value_type, writer=writer)

    # --- write / read ---

    def write(self, key: str, value: Any, writer: str) -> None:
        meta = self._meta.get(key)
        if meta is None:
            raise KeyError(f"Key '{key}' is not registered; call register() first")
        if meta.writer != writer:
            raise PermissionError(
                f"'{writer}' has no write access to '{key}' (owned by '{meta.writer}')"
            )
        if not isinstance(value, meta.value_type):
            raise TypeError(
                f"Key '{key}' expects {meta.value_type.__name__}, "
                f"got {type(value).__name__}"
            )

        with self._lock:
            self._seq += 1
            new_data = {**self._current.data, key: value}
            new_snapshot = Snapshot(
                seq=self._seq,
                ts=time.monotonic(),
                data=new_data,
                changed_key=key,
                writer=writer,
            )
            self._current = new_snapshot
            self._history.append(new_snapshot)

    def read(self, key: str, default: Any = None) -> Any:
        return self._current.data.get(key, default)

    def snapshot(self) -> Snapshot:
        """Return the current immutable snapshot. Cheap — no copy."""
        return self._current

    # --- Event Sourcing Query ---

    def history(self) -> list[Snapshot]:
        return list(self._history)

    def history_of(self, key: str) -> list[Snapshot]:
        """All snapshots in the rolling window where `key` was the changed_key."""
        return [s for s in self._history if s.changed_key == key]

    def values_of(self, key: str, n: int | None = None) -> list[Any]:
        """Recent values written to `key`. Most recent last."""
        snaps = self.history_of(key)
        if n is not None:
            snaps = snaps[-n:]
        return [s.data[key] for s in snaps]

    def changed_between(self, key: str, t0: float, t1: float) -> list[Snapshot]:
        """Snapshots where `key` changed within [t0, t1] (monotonic seconds)."""
        return [s for s in self.history_of(key) if t0 <= s.ts <= t1]

    # --- introspection ---

    def registered_keys(self) -> list[str]:
        return list(self._meta.keys())
