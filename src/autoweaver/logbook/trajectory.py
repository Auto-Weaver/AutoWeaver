"""TrajectoryRecorder — persist per-tick arm state to JSONL for offline analysis.

The kernel already publishes everything an arm knows about itself to the
``WorldBoard`` every tick (Dobot writes ``<arm>.pose`` / ``<arm>.joints`` /
``<arm>.busy`` / ``<arm>.error_code``; other arms publish whatever their
worker declares). What the kernel does *not* do is keep that stream around:
``WorldBoard`` history is a 100-entry in-memory ring with monotonic-only
timestamps, gone on process exit. That's fine for live BT decisions and
useless for "what did the arm actually do during run #47".

``TrajectoryRecorder`` closes that gap. It is a **passive board consumer**:
a Worker that accepts no notes and writes no control state. Each tick it
snapshots the requested namespaces and appends one JSONL line per arm,
stamped with both wall-clock and monotonic time so the trace lines up with
camera frames and logs after the fact.

One ready-made among several, not *the* answer
----------------------------------------------
This recorder samples **on the tick**, and that is one rhythm out of several
legitimate ones. The framework deliberately does not choose: ``Logbook`` and
``Scribe`` provide *writing*, and *when to write* stays with the business.
Somebody who wants 20 Hz regardless of the clock, or a sample per completed
move, opens a thread (or reuses one they already have) and calls
``scribe.write(...)`` on their own schedule — that is not a lesser path, it is
the same path with a different trigger.

Tick sampling has a real drawback worth knowing before picking it: the trace
then shares fate with the clock it is meant to diagnose. If the tick stalls,
the trace thins out at exactly the moment it would have explained why. A
recorder on its own thread keeps sampling through the stall. Choose with that
in mind rather than by which one the framework happened to ship.

Design choices (deliberate):

* **Arm-agnostic.** A *track* is just a namespace string. The recorder
  dumps *every* declared key under it, raw. It does not know what a "pose"
  is, does not extract translation, does not decompose orientation. Raw
  ``WorldBoard`` values go to disk; interpretation belongs to the business
  layer that owns the meaning of those fields.
* **Convention-free serialization.** A 4x4 pose matrix lands as a nested
  list (lossless); a joints tuple lands as a list. No RPY, no quaternions,
  no unit conversion — the bytes you read back are the bytes the board held.
* **Self-describing.** The first line of every file is a ``_meta`` object
  (schema id, tracks, declared keys, hz, start times) so a trace file can
  be read without out-of-band knowledge.
* **Crash-tolerant.** Append-only JSONL with periodic ``flush()``: a hard
  kill loses at most the last unflushed window, never an already-written line.

Wiring (attach the recorder **last** so its tick reads the pose the arm
worker wrote earlier in the *same* tick — ``BTClock`` broadcasts ``on_tick``
in attach order):

    clock.attach_worker(dobot_worker)
    clock.attach_worker(other_arm_worker)
    clock.attach_worker(
        TrajectoryRecorder(tracks=["dobot", "other_arm"], out_dir="trajectories")
    )
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from pathlib import Path
from typing import IO, Any, Sequence

from autoweaver.logbook.serialize import to_jsonable
from autoweaver.worker.base import TickContext
from autoweaver.worker.perception import PerceptionWorker

logger = logging.getLogger(__name__)

#: Deliberately still spells ``telemetry`` after the package was renamed to
#: ``logbook``. A schema id names a **file format**, not a module path, and this
#: format did not change: bumping it would make already-written traces and
#: newly-written ones advertise different schemas while being byte-identical in
#: shape — exactly the confusion a schema id exists to prevent. It changes when
#: the shape of a line changes, and not before.
SCHEMA_ID = "autoweaver.telemetry.trajectory/v1"

#: The package-wide coercion, aliased under this module's historical name so
#: existing importers keep working. Behaviour is unchanged except that
#: dataclasses now become dicts instead of falling through to ``repr``.
_to_jsonable = to_jsonable


class TrajectoryRecorder(PerceptionWorker):
    """Append per-tick state of one or more arm namespaces to rolling JSONL files.

    Args:
        tracks: Namespace(s) to record — a single worker name (``"dobot"``)
            or a sequence of them. Every declared state key under each
            namespace is captured; the recorder makes no assumption about
            which arm or which fields.
        name: This recorder's worker name / WorldBoard namespace.
        out_dir: Directory for trace files (created if missing).
        max_bytes: Roll to a new file once the current one reaches this size.
        decimate: Record every ``decimate``-th tick (1 = every tick). Use to
            trim a 50 Hz stream down without touching the clock.
        only_on_change: When True, skip a track's line if its state is byte-for-byte
            identical to the last one written (drops dupes while the arm is
            parked or when a feedback frame was skipped). When False (default),
            every sampled tick is written — a uniform time grid, easiest to analyse.
        flush_every: Flush the OS buffer (not fsync) every this many written
            lines, bounding crash loss while keeping tick-thread cost low.
    """

    def __init__(
        self,
        tracks: str | Sequence[str],
        *,
        name: str = "traj_recorder",
        out_dir: str | Path = "trajectories",
        max_bytes: int = 64 * 1024 * 1024,
        decimate: int = 1,
        only_on_change: bool = False,
        flush_every: int = 50,
    ) -> None:
        super().__init__()
        self._name = name
        self._tracks: list[str] = [tracks] if isinstance(tracks, str) else list(tracks)
        if not self._tracks:
            raise ValueError("TrajectoryRecorder needs at least one track namespace")
        # Resolve to an absolute path now: a relative out_dir would otherwise
        # land "wherever the process was launched", which is hostile to
        # after-the-fact analysis. ~ is expanded; resolution is lexical so a
        # not-yet-existing directory is fine.
        self._out_dir = Path(out_dir).expanduser().resolve()
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive, got {max_bytes}")
        self._max_bytes = max_bytes
        if decimate < 1:
            raise ValueError(f"decimate must be >= 1, got {decimate}")
        self._decimate = decimate
        self._only_on_change = only_on_change
        self._flush_every = max(1, flush_every)

        # Runtime state (set in on_start, mutated on the tick thread only).
        self._file: IO[str] | None = None
        self._session: str = ""
        self._part: int = 0
        self._bytes: int = 0
        self._samples: int = 0
        self._since_flush: int = 0
        # wall-clock anchored to the monotonic clock so every sample's
        # t_wall is derived from its tick timestamp (no per-line clock skew).
        self._wall0: float = 0.0
        self._mono0: float = 0.0
        # Last serialized state per track, for only_on_change dedupe.
        self._last_state: dict[str, str] = {}

    @property
    def name(self) -> str:
        return self._name

    # Keys recognised by ``from_config`` — mirror the constructor.
    _CONFIG_KEYS = frozenset(
        {"tracks", "name", "out_dir", "max_bytes", "decimate", "only_on_change",
         "flush_every"}
    )

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "TrajectoryRecorder":
        """Build a recorder from a business config mapping.

        The recorder does **not** read YAML itself — the business layer owns
        its config file and hands the relevant section here::

            import yaml
            cfg = yaml.safe_load(open("cell.yaml"))
            recorder = TrajectoryRecorder.from_config(cfg["trajectory"])

        with, e.g.::

            # cell.yaml
            trajectory:
              tracks: [dobot, ls6_1]      # required — arm namespaces to record
              out_dir: /data/runs/traj    # absolute path strongly recommended
              decimate: 1
              only_on_change: false

        Recognised keys mirror the constructor; ``tracks`` is required.
        Unknown keys raise so a YAML typo fails loud at load instead of
        silently recording with defaults.
        """
        if not isinstance(config, dict):
            raise ValueError(
                f"trajectory config must be a mapping, got {type(config).__name__}"
            )
        unknown = set(config) - cls._CONFIG_KEYS
        if unknown:
            raise ValueError(
                f"unknown trajectory config key(s): {sorted(unknown)}; "
                f"recognised keys are {sorted(cls._CONFIG_KEYS)}"
            )
        if "tracks" not in config:
            raise ValueError("trajectory config must specify 'tracks'")
        kwargs = {k: v for k, v in config.items() if k != "tracks"}
        return cls(config["tracks"], **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_attach(self) -> None:
        # The recorder's own bookkeeping — handy to watch live, but it
        # writes no control state and accepts no notes.
        self.declare_state(f"{self._name}.samples", int)
        self.declare_state(f"{self._name}.path", str)
        self.declare_state(f"{self._name}.parts", int)
        self.write_state(f"{self._name}.samples", 0)
        self.write_state(f"{self._name}.path", "")
        self.write_state(f"{self._name}.parts", 0)

    def on_start(self) -> None:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._mono0 = time.monotonic()
        self._wall0 = time.time()
        self._session = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._open_part(0)

    def on_stop(self) -> None:
        self._close_file()

    # ------------------------------------------------------------------
    # Per-tick recording
    # ------------------------------------------------------------------

    def on_tick(self, ctx: TickContext) -> None:
        if self._file is None:
            return
        if self._decimate > 1 and ctx.tick_id % self._decimate != 0:
            return

        # Snapshot once: a consistent, copy-free view of all board state
        # this tick. Attach order guarantees arm writes already landed.
        assert self._board is not None
        data = self._board.snapshot().data
        t_wall = self._wall0 + (ctx.timestamp - self._mono0)

        for ns in self._tracks:
            state = self._collect(ns, data)
            if not state:
                continue  # arm hasn't published anything under this namespace yet
            if self._only_on_change:
                state_json = json.dumps(state, separators=(",", ":"), sort_keys=True)
                if self._last_state.get(ns) == state_json:
                    continue
                self._last_state[ns] = state_json
            record = {
                "t_wall": t_wall,
                "t_mono": ctx.timestamp,
                "tick": ctx.tick_id,
                "ns": ns,
                "state": state,
            }
            self._record(json.dumps(record, separators=(",", ":")) + "\n")

        if self._since_flush >= self._flush_every:
            self._do_flush()

    @staticmethod
    def _collect(ns: str, data: dict[str, Any]) -> dict[str, Any]:
        """Pull every ``<ns>.*`` key out of a board snapshot, prefix stripped."""
        prefix = ns + "."
        out: dict[str, Any] = {}
        for key, value in data.items():
            if key.startswith(prefix):
                out[key[len(prefix):]] = _to_jsonable(value)
        return out

    # ------------------------------------------------------------------
    # File handling (tick thread only)
    # ------------------------------------------------------------------

    def _open_part(self, part: int) -> None:
        self._part = part
        path = self._out_dir / f"{self._name}-{self._session}-{part:03d}.jsonl"
        self._file = path.open("w", encoding="utf-8")
        self._bytes = 0
        header = {
            "_meta": {
                "schema": SCHEMA_ID,
                "recorder": self._name,
                "tracks": self._tracks,
                "track_keys": self._declared_keys(),
                "part": part,
                "t_wall_start": self._wall0,
                "t_mono_start": self._mono0,
                "note": (
                    "values are raw WorldBoard state, prefix-stripped; "
                    "interpretation (units, frames, pose convention) is the "
                    "consumer's responsibility"
                ),
            }
        }
        # Header bypasses the roll check: a max_bytes smaller than the
        # header must not trigger an open→roll→open recursion.
        self._emit(json.dumps(header, separators=(",", ":")) + "\n")
        self.write_state(f"{self._name}.path", str(path))
        self.write_state(f"{self._name}.parts", part + 1)
        logger.info("TrajectoryRecorder '%s' writing %s", self._name, path)

    def _declared_keys(self) -> dict[str, list[str]]:
        """Best-effort schema: declared keys per track at start (attach order
        means the arm workers have already declared theirs)."""
        assert self._board is not None
        declared = self._board.declared_states()
        result: dict[str, list[str]] = {}
        for ns in self._tracks:
            prefix = ns + "."
            result[ns] = sorted(
                k[len(prefix):] for k in declared if k.startswith(prefix)
            )
        return result

    def _emit(self, line: str) -> None:
        """Raw write + byte accounting. No sample count, no roll check."""
        assert self._file is not None
        self._file.write(line)
        self._bytes += len(line.encode("utf-8"))

    def _record(self, line: str) -> None:
        """Write one data line, count it, and roll once the file is full."""
        self._emit(line)
        self._samples += 1
        self._since_flush += 1
        if self._bytes >= self._max_bytes:
            self._roll()

    def _roll(self) -> None:
        self._close_file()  # flushes + closes the full part
        self._open_part(self._part + 1)
        self._since_flush = 0
        self.write_state(f"{self._name}.samples", self._samples)

    def _do_flush(self) -> None:
        if self._file is not None:
            self._file.flush()
        self._since_flush = 0
        self.write_state(f"{self._name}.samples", self._samples)

    def _close_file(self) -> None:
        if self._file is None:
            return
        try:
            self._file.flush()
            self._file.close()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            logger.exception("TrajectoryRecorder '%s' close raised", self._name)
        finally:
            self._file = None
