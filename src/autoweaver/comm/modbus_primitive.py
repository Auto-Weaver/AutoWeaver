"""Comm primitives — the declarative-communication engine. See EVO-009.

Three fixed step operators — ``write`` / ``read`` / ``read_until`` — plus an
action interpreter that runs a declared list of steps as one atomic
transaction. The engine is **hardware-free**: it talks to an abstract
``RegisterIO`` (read/write registers, read/write REAL32 blocks), so the whole
thing is unit-testable against an in-memory fake. The concrete pymodbus-backed
``RegisterIO`` lives one layer down (``ModbusProtocol``); this file knows
nothing about TCP, pymodbus, or any specific rig.

Design law (EVO-009):
  - Primitives are atomic: each does one definite thing, no business branch,
    no mode switch.
  - The engine only executes a declared step list in order. No "if ... then"
    lives here.
  - ``read`` is the degenerate case of ``read_until`` (predicate always true,
    read once) — they share the poll loop but stay two named operators because
    "fetch a value" and "block until a condition" read very differently in a
    declared action.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Transport seam: the engine depends only on this. Concrete impls (pymodbus,
# or an in-memory fake for tests) provide register / REAL32-block I/O.
# --------------------------------------------------------------------------- #
class RegisterIO(ABC):
    """Abstract register transport the primitive engine runs on.

    Deliberately tiny — just enough for ``write`` / ``read`` / ``read_until``
    to operate. Word/byte order for REAL32 is the implementation's concern, so
    the engine never sees raw words: it asks for floats and gets floats.
    """

    @abstractmethod
    def read_u16(self, register: int) -> int:
        """Read one 16-bit holding register."""

    @abstractmethod
    def write_u16(self, register: int, value: int) -> None:
        """Write one 16-bit holding register."""

    @abstractmethod
    def read_real32_block(self, start: int, count: int) -> list[float]:
        """Read ``count`` consecutive REAL32 values (each 2 registers)."""

    @abstractmethod
    def write_real32_block(self, start: int, values: Sequence[float]) -> None:
        """Write a block of REAL32 values starting at ``start``."""


# --------------------------------------------------------------------------- #
# Clock seam: read_until needs "now" + "sleep a bit". Injected so tests can
# drive time deterministically instead of wall-clock sleeping.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Clock:
    """Monotonic time + sleep, injectable for deterministic tests."""

    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class CommActionError(RuntimeError):
    """Base for engine-level action failures."""


class ReadUntilTimeout(CommActionError):
    """A ``read_until`` step did not see its predicate satisfied in time."""

    def __init__(self, register: int, expected: Any, last_seen: Any, timeout_s: float):
        self.register = register
        self.expected = expected
        self.last_seen = last_seen
        self.timeout_s = timeout_s
        super().__init__(
            f"read_until timed out after {timeout_s}s: register {register} "
            f"expected {expected!r}, last saw {last_seen!r}"
        )


class ActionStepError(CommActionError):
    """A step was malformed (unknown verb / missing field). Should normally be
    caught at contract-load time, but the engine guards anyway."""


# --------------------------------------------------------------------------- #
# Resolved contract pieces the engine consumes. These are produced by the
# (validated) loader — see EVO-009 §Schema. The engine takes them as data; it
# does not parse YAML.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BlockSpec:
    """A REAL32 block: where it starts, how many values, and the wire order
    (a permutation of x,y,z,rx,ry,rz the rig expects in its registers)."""

    start: int
    count: int
    order: tuple[str, ...]


# Canonical pose field order used everywhere in pose math.
POSE_FIELDS: tuple[str, ...] = ("x", "y", "z", "rx", "ry", "rz")


@dataclass(frozen=True)
class CommContract:
    """The data half of a rig: named registers, named REAL32 blocks, and the
    flag/func constant tables. Pure lookup tables — no behaviour.

    ``registers`` maps a name (``"plc_send"``) to an address.
    ``blocks`` maps a name (``"cmd_pose"``) to a BlockSpec.
    ``constants`` maps a symbolic value name (``"SET"``, ``"COORD"``) to its
    integer (so steps can say ``equals: CLEAR`` instead of a magic 0).
    """

    registers: Mapping[str, int]
    blocks: Mapping[str, BlockSpec]
    constants: Mapping[str, int]

    def reg(self, name: str) -> int:
        try:
            return self.registers[name]
        except KeyError:
            raise ActionStepError(f"unknown register name {name!r}") from None

    def block(self, name: str) -> BlockSpec:
        try:
            return self.blocks[name]
        except KeyError:
            raise ActionStepError(f"unknown block name {name!r}") from None

    def const(self, value: Any) -> int:
        """Resolve a value that may be a symbolic constant name or a raw int."""
        if isinstance(value, str):
            try:
                return self.constants[value]
            except KeyError:
                raise ActionStepError(
                    f"unknown constant {value!r}; declare it in func_codes/flags"
                ) from None
        return int(value)


# --------------------------------------------------------------------------- #
# Steps — the three primitives, as plain data the engine dispatches on.
# A step is a one-key dict: {"write": {...}} / {"read": {...}} /
# {"read_until": {...}}. The loader builds these; the engine executes them.
# --------------------------------------------------------------------------- #
def _reorder_pose(values: Mapping[str, float], order: Sequence[str]) -> list[float]:
    """Map canonical-field pose values onto the rig's wire order."""
    norm = tuple(str(f).strip().lower() for f in order)
    if len(norm) != len(POSE_FIELDS) or sorted(norm) != sorted(POSE_FIELDS):
        raise ActionStepError(
            f"block order must be a permutation of {POSE_FIELDS}, got {tuple(order)!r}"
        )
    missing = [f for f in norm if f not in values]
    if missing:
        raise ActionStepError(f"pose values missing fields {missing}")
    return [float(values[name]) for name in norm]


class CommEngine:
    """Executes declared actions over a RegisterIO. Stateless across actions.

    One engine instance binds a contract + a transport. ``run_action`` runs
    one action's step list to completion (atomically, blocking the calling
    thread — callers run it via ``run_async`` so the BT tick never blocks; see
    EVO-009 §Execution). It returns a dict of any values produced by ``read``
    steps, keyed by the step's ``into`` name.
    """

    def __init__(
        self,
        contract: CommContract,
        io: RegisterIO,
        *,
        clock: Clock | None = None,
        poll_interval_s: float = 0.05,
    ) -> None:
        self._c = contract
        self._io = io
        self._clock = clock or Clock()
        self._poll_interval_s = float(poll_interval_s)

    # -- public ------------------------------------------------------------ #
    def run_action(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run an action: execute each step in order. Returns read results.

        ``params`` carries runtime injections referenced by ``$name`` in a
        step (e.g. ``$pose`` for ``move_pose``). Any step raising aborts the
        action — the transaction is all-or-nothing from the caller's view; the
        caller (Worker) turns the exception into an ``error`` state.
        """
        params = params or {}
        results: dict[str, Any] = {}
        for index, step in enumerate(steps):
            if len(step) != 1:
                raise ActionStepError(
                    f"step {index} must have exactly one verb, got {list(step)}"
                )
            (verb, spec), = step.items()
            handler = self._VERBS.get(verb)
            if handler is None:
                raise ActionStepError(f"step {index}: unknown verb {verb!r}")
            handler(self, spec, params, results)
        return results

    # -- primitives -------------------------------------------------------- #
    def _do_write(
        self,
        spec: Mapping[str, Any],
        params: Mapping[str, Any],
        _results: dict[str, Any],
    ) -> None:
        """write: registers / blocks / flags. Fire and return — no read, no
        wait, no judgement.

        A pose block accepts ONE pose (a field->value Mapping) or a PATH of poses
        (a sequence of such Mappings). The path form writes the points
        consecutively from the block start — e.g. a multi-waypoint trajectory
        sent in one transaction. It is still the `write` primitive: fire every
        point and return; no read, no branch."""
        if "block" in spec:
            block = self._c.block(spec["block"])
            values = self._resolve(spec["values"], params)
            points = list(values) if isinstance(values, (list, tuple)) else [values]
            if not points:
                raise ActionStepError(
                    f"write block {spec['block']!r} got an empty path (no points)"
                )
            flat: list[float] = []
            for i, point in enumerate(points):
                if not isinstance(point, Mapping):
                    raise ActionStepError(
                        f"write block {spec['block']!r} needs a field->value mapping "
                        f"(or a list of them); point {i} is {type(point).__name__}"
                    )
                ordered = _reorder_pose(point, block.order)
                if len(ordered) != block.count:
                    raise ActionStepError(
                        f"block {spec['block']!r} expects {block.count} values per "
                        f"point, got {len(ordered)}"
                    )
                flat.extend(ordered)
            self._io.write_real32_block(block.start, flat)
            return

        if "flags" in spec:
            for name, value in spec["flags"].items():
                self._io.write_u16(self._c.reg(name), self._c.const(value))
            return

        if "register" in spec:
            self._io.write_u16(self._c.reg(spec["register"]), self._c.const(spec["value"]))
            return

        raise ActionStepError(f"write step has no target (block/flags/register): {spec}")

    def _do_read(
        self,
        spec: Mapping[str, Any],
        _params: Mapping[str, Any],
        results: dict[str, Any],
    ) -> None:
        """read: fetch once, store under ``into`` (default 'value')."""
        into = spec.get("into", "value")
        if "block" in spec:
            block = self._c.block(spec["block"])
            raw = self._io.read_real32_block(block.start, block.count)
            results[into] = self._block_to_fields(raw, block.order)
            return
        if "register" in spec:
            results[into] = self._io.read_u16(self._c.reg(spec["register"]))
            return
        raise ActionStepError(f"read step has no source (block/register): {spec}")

    def _do_read_until(
        self,
        spec: Mapping[str, Any],
        _params: Mapping[str, Any],
        _results: dict[str, Any],
    ) -> None:
        """read_until: poll a register until it equals the expected value, or
        time out. The predicate is data (``equals: <const>``), not code."""
        register = self._c.reg(spec["register"])
        expected = self._c.const(spec["equals"])
        timeout_s = float(spec["timeout_s"])
        deadline = self._clock.monotonic() + timeout_s
        last = None
        while True:
            last = self._io.read_u16(register)
            if last == expected:
                return
            if self._clock.monotonic() >= deadline:
                raise ReadUntilTimeout(register, expected, last, timeout_s)
            self._clock.sleep(self._poll_interval_s)

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _resolve(value: Any, params: Mapping[str, Any]) -> Any:
        """Resolve a ``$name`` reference against runtime params; pass through
        anything else unchanged."""
        if isinstance(value, str) and value.startswith("$"):
            key = value[1:]
            try:
                return params[key]
            except KeyError:
                raise ActionStepError(
                    f"step references ${key} but it was not supplied at run time"
                ) from None
        return value

    @staticmethod
    def _block_to_fields(raw: Sequence[float], order: Sequence[str]) -> dict[str, float]:
        """Map a wire-order REAL32 block back to canonical field names."""
        norm = tuple(str(f).strip().lower() for f in order)
        return {name: float(v) for name, v in zip(norm, raw)}

    # verb dispatch table — the only place the three primitives are named.
    _VERBS: dict[str, Callable[..., None]] = {
        "write": _do_write,
        "read": _do_read,
        "read_until": _do_read_until,
    }


__all__ = [
    "RegisterIO",
    "Clock",
    "CommEngine",
    "CommContract",
    "BlockSpec",
    "POSE_FIELDS",
    "CommActionError",
    "ReadUntilTimeout",
    "ActionStepError",
]
