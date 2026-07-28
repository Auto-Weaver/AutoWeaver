"""Sensor base class — the perception layer's single outward-facing door.

A Sensor is a stateful device driver held by a Worker. It does NOT respond to
ticks; it exposes open / close / observe / configure for its driver to call.
See EVO-007 for the Worker model and EVO-011 for the observation model.

Drive chain: **BT -> Sensor -> Observer**
-----------------------------------------
The BT tree is still the system's only active scheduler. The Sensor is passive
towards the clock — *no internal heartbeat, no thread* (other than what the
device SDK requires) — and active towards its observers: having produced an
observation, it pushes it to whoever subscribed.

The three acquisition modes are **BT-side orchestration**, not machinery in here:

  - live       -> the BT observes every N ticks
  - on demand  -> the BT sends a note at the moment worth capturing
  - burst      -> a ``RepeatUntil`` loop (EVO-010) observing while a condition holds

There is deliberately no scheduler, no mode enum and no polling loop in this
class. A Sensor that grew one would be a second heartbeat.

Sensor is the only door
-----------------------
The BT knows a **role name** and says "observe". Observer fan-out, lineage,
imaging conditions — none of it enters the BT's field of view.

Role, not model name
--------------------
``name`` historically defaults to the class name, which makes two Daheng cameras
both ``"DahengCamera"`` — the framework could not express *which device is which*,
so every project reinvented it. :attr:`role` is the device's **position in the
system** (``"nest"`` / ``"drill"``), declared by assembly. ``observe()`` refuses
to run without one: silently falling back to the class name would recreate the
exact defect this exists to remove.

Backward compatibility
----------------------
``observe()`` is **added**, not swapped in. ``snapshot()`` keeps working
unchanged, as do ``CameraBase.capture()`` / ``is_opened()``. Existing callers are
untouched by this cut.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, List, Mapping, Optional, Tuple

from autoweaver.sensor.observation import Observation
from autoweaver.sensor.observer import Observer, ObserverSpeed

logger = logging.getLogger(__name__)

#: Guards lazy creation of per-instance state. Subclasses predating this cut do
#: not call ``super().__init__()`` (``MockCamera``, ``DahengCamera``), so the
#: lock and the observer list have to be created on first use — and that creation
#: must itself be safe if two threads race into it.
_INIT_LOCK = threading.Lock()

#: Submits a callable to run off the caller's thread (``run_async`` and friends).
SlowDispatcher = Callable[[Callable[[], None]], Any]


class Sensor(ABC):
    """Abstract device driver.

    Workers hold one or more Sensors. Lifecycle and reading:

      - ``open / close``  — called by the Worker's on_start / on_stop
      - ``is_open``       — query
      - ``observe``       — take one reading **and mint an Observation** for it
      - ``snapshot``      — the raw, unnamed reading (pre-EVO-011 contract)
      - ``configure``     — set device parameters

    ``observe`` / ``snapshot`` may be slow (waiting on a fresh frame). Callers are
    responsible for going through ``run_async`` if a read does not fit the tick
    budget.
    """

    def __init__(self, *, role: Optional[str] = None) -> None:
        """New code may declare the role here; existing subclasses need not call
        this at all — every attribute it sets is also created lazily."""
        self._role = role

    # -- identity ---------------------------------------------------------- #

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier for logs and metrics."""

    @property
    def role(self) -> Optional[str]:
        """This device's position in the system, e.g. ``"nest"`` / ``"drill"``.

        ``None`` until assembly declares it. Deliberately distinct from
        :attr:`name`: the model of camera is not its job.
        """
        return getattr(self, "_role", None)

    @role.setter
    def role(self, value: str) -> None:
        if not value or not str(value).strip():
            raise ValueError("sensor role must be a non-empty string")
        self._role = str(value)

    @property
    def projection(self) -> Any:
        """Reference to this source's optical / calibration model, or ``None``.

        Opaque to the framework — stamped onto every observation so the model
        travels with the data instead of being looked up separately by each
        consumer. Interpretation belongs to the business layer.
        """
        return getattr(self, "_projection", None)

    @projection.setter
    def projection(self, value: Any) -> None:
        self._projection = value

    # -- lifecycle --------------------------------------------------------- #

    @abstractmethod
    def open(self) -> None:
        """Acquire device resources. Idempotent if already open."""

    @abstractmethod
    def close(self) -> None:
        """Release device resources. Idempotent if already closed."""

    @abstractmethod
    def is_open(self) -> bool:
        """Whether the device is currently open and ready."""

    @abstractmethod
    def snapshot(self) -> Any:
        """Return the current raw reading.

        For triggered devices (camera): captures and returns a fresh sample.
        For continuous devices (pressure, distance): returns the latest value.

        Implementations should raise rather than return stale / sentinel values
        when the device is unavailable.

        This is the **pre-EVO-011 contract** and returns a bare value with no
        identity attached. It is kept working for existing callers; new code
        should call :meth:`observe`.
        """

    def configure(self, **kwargs: Any) -> None:
        """Apply device parameters. Default no-op; subclasses override if their
        device exposes configuration."""

    # -- observers --------------------------------------------------------- #

    def add_observer(
        self, observer: Observer, *, speed: Optional[ObserverSpeed] = None
    ) -> None:
        """Subscribe ``observer`` to this sensor's observations.

        ``speed`` defaults to the observer's own declaration; passing it here
        overrides for this subscription. Registering a ``SLOW`` observer without
        a dispatcher raises **now**, rather than stalling the tick later.
        """
        resolved = speed if speed is not None else observer.speed
        if not isinstance(resolved, ObserverSpeed):
            raise TypeError(
                f"observer {observer!r} must declare an ObserverSpeed, got {resolved!r}"
            )
        if resolved is ObserverSpeed.SLOW and self._slow_dispatcher is None:
            raise RuntimeError(
                f"observer '{getattr(observer, 'name', observer)}' is SLOW but sensor "
                f"'{self.name}' has no slow dispatcher; call set_slow_dispatcher() "
                "first (run_async / run_background) — a slow observer must never run "
                "on the tick thread"
            )
        entries = self._observer_entries()
        with self._registry_lock:
            if any(existing is observer for existing, _ in entries):
                return
            entries.append((observer, resolved))

    def remove_observer(self, observer: Observer) -> None:
        """Unsubscribe ``observer``. Silently ignores one that is not subscribed."""
        entries = self._observer_entries()
        with self._registry_lock:
            for index, (existing, _) in enumerate(entries):
                if existing is observer:
                    del entries[index]
                    return

    @property
    def observers(self) -> Tuple[Observer, ...]:
        """Currently subscribed observers, in registration order."""
        return tuple(observer for observer, _ in self._observer_entries())

    def set_slow_dispatcher(self, dispatcher: Optional[SlowDispatcher]) -> None:
        """Supply the hand-off used for ``SLOW`` observers.

        The Sensor never creates a thread of its own; it delegates. Assembly
        wires in ``run_async`` / ``run_background``.
        """
        self._slow_dispatcher_fn = dispatcher

    @property
    def _slow_dispatcher(self) -> Optional[SlowDispatcher]:
        return getattr(self, "_slow_dispatcher_fn", None)

    # -- observing --------------------------------------------------------- #

    def observe(self) -> Observation:
        """Take one reading, mint an :class:`Observation`, push it to observers.

        Serialised per instance by a lock — **a lock, not a thread**. This is
        what removes the hand-written arbitration that projects otherwise grow
        (a ``ThreadSafe*`` wrapper plus ad-hoc "yield to the burst" logic): one
        device has one driver, and concurrent callers queue.

        Fan-out happens **after** the lock is released, so a slow observer can
        never block the device. The trade-off: with genuinely concurrent
        ``observe()`` calls, delivery order is not guaranteed. Under the BT —
        the only driver — that does not arise.
        """
        role = self.role
        if not role:
            raise ValueError(
                f"sensor '{self.name}' has no role; assign one before observe() "
                "(e.g. sensor.role = 'nest'). The role is the device's position "
                "in the system, and observations are identified by it"
            )
        with self._observe_lock:
            payload = self._read_payload()
            captured_at = time.monotonic()
            observation_id = self._next_observation_id()
            observation = self._build_observation(
                observation_id=observation_id,
                source=role,
                captured_at=captured_at,
                payload=payload,
            )
        self._publish(observation)
        return observation

    # -- extension points -------------------------------------------------- #

    def _read_payload(self) -> Any:
        """Fetch the raw reading. Override only if ``snapshot()`` is not it."""
        return self.snapshot()

    def _observation_conditions(self) -> Mapping[str, Any]:
        """Acquisition conditions only this device knows. Empty by default."""
        return {}

    def _build_observation(
        self, *, observation_id: int, source: str, captured_at: float, payload: Any
    ) -> Observation:
        """Mint the Observation. Subclasses return their own richer type."""
        return Observation(
            id=observation_id,
            source=source,
            captured_at=captured_at,
            data=payload,
            conditions=self._observation_conditions(),
            projection=self.projection,
        )

    # -- internals --------------------------------------------------------- #

    def _lazy_lock(self, attribute: str) -> threading.Lock:
        """Fetch (creating on first use) a per-instance lock.

        Lazy because subclasses predating this cut never call
        ``super().__init__()``; double-checked under ``_INIT_LOCK`` so two
        threads racing into first use cannot end up with two different locks.
        """
        lock = getattr(self, attribute, None)
        if lock is None:
            with _INIT_LOCK:
                lock = getattr(self, attribute, None)
                if lock is None:
                    lock = threading.Lock()
                    setattr(self, attribute, lock)
        return lock

    @property
    def _observe_lock(self) -> threading.Lock:
        """Serialises device reads — one device, one driver at a time."""
        return self._lazy_lock("_observe_lock_obj")

    @property
    def _registry_lock(self) -> threading.Lock:
        """Guards the observer list. Separate from :attr:`_observe_lock` so
        subscribing never has to wait behind a slow device read."""
        return self._lazy_lock("_registry_lock_obj")

    def _observer_entries(self) -> List[Tuple[Observer, ObserverSpeed]]:
        entries = getattr(self, "_observer_list", None)
        if entries is None:
            with _INIT_LOCK:
                entries = getattr(self, "_observer_list", None)
                if entries is None:
                    entries = []
                    self._observer_list = entries
        return entries

    def _next_observation_id(self) -> int:
        """Monotonic per instance, hence per role. Starts at 1, so a default 0
        anywhere downstream reads as "nothing observed yet"."""
        current = getattr(self, "_observation_seq", 0) + 1
        self._observation_seq = current
        return current

    def _publish(self, observation: Observation) -> None:
        """Fan out to observers. One observer raising does not stop the others,
        and never propagates into the device read (``architecture.md``'s fault
        isolation) — but it is logged, not swallowed."""
        for observer, speed in list(self._observer_entries()):
            if speed is ObserverSpeed.SLOW:
                dispatcher = self._slow_dispatcher
                if dispatcher is None:  # pragma: no cover - blocked at registration
                    logger.error(
                        "sensor '%s': slow observer '%s' has no dispatcher; skipping",
                        self.name, getattr(observer, "name", observer),
                    )
                    continue
                self._dispatch_slow(dispatcher, observer, observation)
            else:
                self._invoke(observer, observation)

    def _dispatch_slow(
        self, dispatcher: SlowDispatcher, observer: Observer, observation: Observation
    ) -> None:
        try:
            dispatcher(lambda: self._invoke(observer, observation))
        except Exception:  # noqa: BLE001 - a broken dispatcher must not kill the read
            logger.exception(
                "sensor '%s': failed to dispatch observation to slow observer '%s'",
                self.name, getattr(observer, "name", observer),
            )

    def _invoke(self, observer: Observer, observation: Observation) -> None:
        try:
            observer.on_observation(observation)
        except Exception:  # noqa: BLE001 - a crashing preview must not kill perception
            logger.exception(
                "sensor '%s': observer '%s' raised on observation %d",
                self.name, getattr(observer, "name", observer), observation.id,
            )


__all__ = ["Sensor", "SlowDispatcher"]
