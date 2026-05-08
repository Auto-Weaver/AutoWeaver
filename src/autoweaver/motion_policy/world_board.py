from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable


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
class _StateMeta:
    value_type: type
    writer: str


@dataclass
class _NoteAcceptor:
    payload_type: type
    on_receive: Callable[[Any], None]


@dataclass
class _PendingNote:
    namespace: str
    name: str
    payload: Any
    sender: str


def _split_namespace(key: str) -> tuple[str, str]:
    """Split a key into (namespace, rest). Raises ValueError if no namespace.

    Namespace is the first dot-separated segment. The rest may contain further dots.
    """
    if "." not in key:
        raise ValueError(
            f"Key '{key}' has no namespace. WorldBoard state keys must be of the "
            "form '<namespace>.<rest>' (e.g. 'perception.detections')."
        )
    namespace, rest = key.split(".", 1)
    if not namespace or not rest:
        raise ValueError(
            f"Key '{key}' has empty namespace or rest segment."
        )
    return namespace, rest


class WorldBoard:
    """Process-wide observable state with immutable snapshots and rolling history.

    Concurrency model: device threads write, BT main thread reads.
    Writes replace the snapshot ref under a small lock. Reads return the
    current immutable Snapshot — no lock needed because the ref read itself
    is atomic under the GIL.

    History is a sliding window of past snapshot refs (default 100). Each
    state write produces a new ref; old refs survive until evicted.

    Two kinds of things live here:

    State (the bulletin board)
    --------------------------
    Persistent fields written by Subsystems under their own namespace.
    Each key is of the form ``<namespace>.<rest>``. A namespace is owned
    by exactly one writer — once any key in ``foo.*`` is declared with
    writer ``X``, every other ``foo.*`` key must also be declared (and
    written) by ``X``. State is what's *currently true* about the world.

        declare_state(key, value_type, writer)    — claim a state field
        post_state(key, value, writer)            — publish a new value
        read_state(key, default=None)             — read current value

    Notes (passed slips of paper)
    -----------------------------
    One-shot, one-way slips passed to a Subsystem from outside (typically
    by a BT NotifyLeaf). Notes do NOT enter the state snapshot — they sit
    in a pending queue until ``deliver_notes()`` runs the registered
    receiver(s). After delivery they're gone.

    Multiple notes passed to the same (namespace, name) within a single
    delivery cycle are all delivered, in pass order — none are dropped or
    coalesced.

        accept_notes(namespace, name, payload_type, on_receive)
                                                  — declare ability to receive
        pass_note(namespace, name, payload, sender)
                                                  — send a slip
        deliver_notes()                           — flush the pending queue
        accepted_notes()                          — list (namespace, name) pairs
                                                    that have a receiver

    BT Clock calls ``deliver_notes()`` at the start of each tick before
    broadcasting tick to subsystems.
    """

    DEFAULT_HISTORY_SIZE = 100

    def __init__(self, history_size: int = DEFAULT_HISTORY_SIZE):
        self._state_meta: dict[str, _StateMeta] = {}
        self._namespace_owners: dict[str, str] = {}
        self._note_acceptors: dict[tuple[str, str], _NoteAcceptor] = {}
        self._pending_notes: deque[_PendingNote] = deque()
        self._lock = threading.Lock()
        self._seq = 0
        empty = Snapshot(seq=0, ts=time.monotonic(), data={})
        self._current: Snapshot = empty
        self._history: deque[Snapshot] = deque(maxlen=history_size)
        self._history.append(empty)

    # ------------------------------------------------------------------
    # State (bulletin board)
    # ------------------------------------------------------------------

    def declare_state(self, key: str, value_type: type, writer: str) -> None:
        """Claim a state field at ``<namespace>.<rest>``.

        Raises:
            ValueError: key has no namespace, namespace is owned by a
                different writer, or key already declared with a different
                writer.
        """
        namespace, _rest = _split_namespace(key)
        with self._lock:
            self._claim_namespace(namespace, writer)
            existing = self._state_meta.get(key)
            if existing is not None and existing.writer != writer:
                raise ValueError(
                    f"State '{key}' already declared by '{existing.writer}', "
                    f"cannot redeclare with writer '{writer}'"
                )
            self._state_meta[key] = _StateMeta(value_type=value_type, writer=writer)

    def post_state(self, key: str, value: Any, writer: str) -> None:
        """Publish a new value to a declared state field."""
        meta = self._state_meta.get(key)
        if meta is None:
            raise KeyError(
                f"State '{key}' is not declared; call declare_state() first"
            )
        if meta.writer != writer:
            raise PermissionError(
                f"'{writer}' has no write access to '{key}' "
                f"(owned by '{meta.writer}')"
            )
        if not isinstance(value, meta.value_type):
            raise TypeError(
                f"State '{key}' expects {meta.value_type.__name__}, "
                f"got {type(value).__name__}"
            )
        self._commit(key, value, writer)

    def read_state(self, key: str, default: Any = None) -> Any:
        return self._current.data.get(key, default)

    # ------------------------------------------------------------------
    # Notes (one-shot slips)
    # ------------------------------------------------------------------

    def accept_notes(
        self,
        namespace: str,
        name: str,
        payload_type: type,
        on_receive: Callable[[Any], None],
    ) -> None:
        """Declare that the caller will receive notes of (namespace, name).

        Args:
            namespace: namespace of the receiver. By convention this is the
                receiving Subsystem's name.
            name: short note name (e.g. ``"start_picking"``). Must not
                contain dots.
            payload_type: expected payload type; checked at ``pass_note``.
            on_receive: callable invoked once per delivered note, in pass
                order, when ``deliver_notes()`` runs.

        Raises:
            ValueError: ``name`` contains a dot; or (namespace, name) already
                has a receiver.
        """
        if "." in name:
            raise ValueError(
                f"Note name '{name}' must not contain dots."
            )
        key = (namespace, name)
        with self._lock:
            if key in self._note_acceptors:
                raise ValueError(
                    f"Note ({namespace!r}, {name!r}) already has a receiver."
                )
            self._note_acceptors[key] = _NoteAcceptor(
                payload_type=payload_type,
                on_receive=on_receive,
            )

    def pass_note(
        self,
        namespace: str,
        name: str,
        payload: Any,
        sender: str,
    ) -> None:
        """Pass a one-shot slip to (namespace, name).

        The note enters a pending queue. It is *not* visible via
        ``read_state`` and does not produce a Snapshot. The receiver's
        ``on_receive`` runs at the next ``deliver_notes()`` call.

        Multiple notes to the same (namespace, name) within a single
        delivery cycle are all delivered, in pass order.

        Raises:
            KeyError: no receiver registered for (namespace, name).
            TypeError: payload type mismatch.
        """
        acceptor = self._note_acceptors.get((namespace, name))
        if acceptor is None:
            raise KeyError(
                f"No receiver for note ({namespace!r}, {name!r}). "
                "The receiving Subsystem must call accept_notes() first."
            )
        if not isinstance(payload, acceptor.payload_type):
            raise TypeError(
                f"Note ({namespace!r}, {name!r}) expects payload of "
                f"{acceptor.payload_type.__name__}, got {type(payload).__name__}"
            )
        with self._lock:
            self._pending_notes.append(
                _PendingNote(
                    namespace=namespace,
                    name=name,
                    payload=payload,
                    sender=sender,
                )
            )

    def deliver_notes(self) -> None:
        """Deliver all pending notes to their receivers, in pass order.

        BT Clock calls this at the start of each tick.

        Receiver callbacks are invoked synchronously. If a callback raises,
        delivery of the remaining pending notes still proceeds; exceptions
        are collected and re-raised as a single ExceptionGroup at the end
        so that one bad receiver cannot starve the others.
        """
        with self._lock:
            pending = list(self._pending_notes)
            self._pending_notes.clear()

        errors: list[BaseException] = []
        for note in pending:
            acceptor = self._note_acceptors.get((note.namespace, note.name))
            if acceptor is None:
                # Receiver was un-registered between pass and deliver.
                # Drop silently — the sender already returned successfully.
                continue
            try:
                acceptor.on_receive(note.payload)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("note receiver(s) raised", errors)

    def accepted_notes(self) -> list[tuple[str, str]]:
        """Return the list of (namespace, name) pairs with registered receivers."""
        return list(self._note_acceptors.keys())

    # ------------------------------------------------------------------
    # Snapshot / history
    # ------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Return the current immutable state snapshot. Cheap — no copy."""
        return self._current

    def history(self) -> list[Snapshot]:
        return list(self._history)

    def history_of(self, key: str) -> list[Snapshot]:
        """Snapshots in the rolling window where `key` was the changed_key."""
        return [s for s in self._history if s.changed_key == key]

    def values_of(self, key: str, n: int | None = None) -> list[Any]:
        """Recent values posted to `key`. Most recent last."""
        snaps = self.history_of(key)
        if n is not None:
            snaps = snaps[-n:]
        return [s.data[key] for s in snaps]

    def changed_between(self, key: str, t0: float, t1: float) -> list[Snapshot]:
        """Snapshots where `key` changed within [t0, t1] (monotonic seconds)."""
        return [s for s in self.history_of(key) if t0 <= s.ts <= t1]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def declared_states(self) -> list[str]:
        return list(self._state_meta.keys())

    def namespace_owner(self, namespace: str) -> str | None:
        return self._namespace_owners.get(namespace)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _claim_namespace(self, namespace: str, writer: str) -> None:
        """Establish or verify namespace ownership. Caller holds the lock."""
        owner = self._namespace_owners.get(namespace)
        if owner is None:
            self._namespace_owners[namespace] = writer
            return
        if owner != writer:
            raise ValueError(
                f"Namespace '{namespace}' already owned by '{owner}', "
                f"cannot declare key with writer '{writer}'"
            )

    def _commit(self, key: str, value: Any, writer: str) -> None:
        """Append a new snapshot reflecting the state write."""
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
