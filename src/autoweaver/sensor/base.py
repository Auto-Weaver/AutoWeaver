"""Sensor base class — the perception layer's single outward-facing door.

A Sensor is a stateful device driver held by a Worker. It does NOT respond to
ticks; it exposes open / close / observe / configure for its driver to call.
See EVO-007 for the Worker model and EVO-011 for the observation model.

Drive chain: **BT -> Sensor -> Observer**
-----------------------------------------
The BT tree is still the system's only active **control** scheduler, and the
Sensor is active towards its observers: having produced an observation, it pushes
it to whoever subscribed.

.. warning:: RETRACTED 2026-07-27 — superseded by EVO-012 (implemented below)

   This paragraph used to read "The Sensor is passive towards the clock — *no
   internal heartbeat, no thread* (other than what the device SDK requires)".
   **The no-thread clause is retracted**; it is kept, marked, because the
   reasoning was correct about Workers and wrong about devices.

   ``observe()`` is **pull**, so the acquisition rate is bound by the consumer
   (capture -> consume -> capture). A camera is natively push. Continuous
   acquisition therefore needs the Sensor to own an acquisition rhythm — a
   thread. The error was generalising ``architecture.md``'s "no **Worker** may
   keep its own heartbeat" to the device layer: Workers participate in control,
   Sensors do not. What survives, and binds: **an acquisition thread writes no
   control state, sends no notes, participates in no criteria.** The BT remains
   the only *control* scheduler. Full argument and the measured evidence from
   pluck's burst path: ``sensor/observer.py`` module docstring.

   ``observe()`` is **unchanged and still correct** — it is the pull path, kept
   as the "on demand" mode alongside the continuous mode below.

Two modes, both real
--------------------
**On demand** (``observe()``) — the caller asks, one observation comes back.
Right when the world is standing still and the question is "what does it look
like *now*": the arm has settled at a scan pose and the answer wanted is a
freshly acquired frame at a known moment.

**Continuous** (``start_streaming()`` / ``stop_streaming()``) — an acquisition
thread reads at the device's own rhythm and pushes to subscribers. Right when the
world is moving and the consumers cannot all keep up: a burst during a lift, a
live preview, video recording. Nothing downstream can slow acquisition here; a
consumer that falls behind drops observations and says so.

Which mode is in force, and when to switch, is **the caller's** decision — this
class has no scheduler, no mode enum and no policy about it. ``start_streaming``
and ``stop_streaming`` are the whole API surface, and ``observe()`` keeps working
either way (both take the same device lock, so a pull during a stream simply
queues behind the acquisition thread's current read).

The three acquisition modes are **BT-side orchestration**, not machinery in here:

  - live       -> the BT observes every N ticks
  - on demand  -> the BT sends a note at the moment worth capturing
  - burst      -> a ``RepeatUntil`` loop (EVO-010) observing while a condition holds

There is deliberately no scheduler, no mode enum and no polling loop in this
class. A Sensor that grew one would be a second heartbeat.

.. warning:: PARTLY RETRACTED 2026-07-27 — resolved by EVO-012 (implemented below)

   **"burst -> a ``RepeatUntil`` loop"** does not work, and neither does "a
   Sensor that grew a loop would be a second heartbeat". A BT loop cannot fix
   burst: the BT ticks at 50 Hz, whereas burst acquisition has to track the
   camera's frame rate and a displacement gate, and — the actual defect — a pull
   loop still serialises capture behind consumption whichever thread drives it.
   pluck measured 288 ms/frame service time against a 0.05-0.30 s window, and
   its own note records that tightening the gate ("调 lift_move_step_mm") changed
   nothing. See ``sensor/observer.py`` for the numbers.

   **live** and **on demand** remain BT-side orchestration exactly as described.
   Continuous acquisition (which subsumes burst) is now :meth:`Sensor.start_streaming`,
   with an acquisition thread that participates in no control.

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
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from autoweaver.sensor.delivery import DropPolicy, QueuedDelivery
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
        self,
        observer: Observer,
        *,
        speed: Optional[ObserverSpeed] = None,
        capacity: Optional[int] = None,
        policy: Optional[DropPolicy] = None,
    ) -> None:
        """Subscribe ``observer`` to this sensor's observations.

        ``speed`` defaults to the observer's own declaration; passing it here
        overrides for this subscription. Registering a ``SLOW`` observer without
        a dispatcher raises **now**, rather than stalling the tick later.

        **Bounded delivery** (EVO-012) — pass ``capacity`` *and* ``policy`` to give
        this consumer its own bounded queue and delivery thread instead of the
        shared dispatcher. That is what makes it safe under continuous
        acquisition: the producer's cost becomes one non-blocking ``offer``, and a
        consumer that falls behind drops observations under **its own** policy
        rather than throttling the device or growing the heap without limit.

        Both arguments are required together and neither has a default. A
        capacity guessed by the framework would be wrong by construction — one
        frame is 9.4 MB on one of pluck's cameras and 35 MB on the other — and a
        default policy would silently pick which observations a consumer loses,
        which is a business decision (see :class:`DropPolicy`).

        Omit both and the behaviour is exactly as before: ``FAST`` inline on the
        caller's thread, ``SLOW`` through the dispatcher.
        """
        resolved = speed if speed is not None else observer.speed
        if not isinstance(resolved, ObserverSpeed):
            raise TypeError(
                f"observer {observer!r} must declare an ObserverSpeed, got {resolved!r}"
            )
        queued = capacity is not None or policy is not None
        if queued and (capacity is None or policy is None):
            raise ValueError(
                "bounded delivery needs both capacity and policy; passing one "
                "without the other would let the framework invent the half you "
                "left out, and both halves are yours to decide"
            )
        if not queued and resolved is ObserverSpeed.SLOW and self._slow_dispatcher is None:
            raise RuntimeError(
                f"observer '{getattr(observer, 'name', observer)}' is SLOW but sensor "
                f"'{self.name}' has no slow dispatcher; call set_slow_dispatcher() "
                "first (run_async / run_background), or register it with "
                "capacity=/policy= for its own bounded queue — a slow observer "
                "must never run on the tick thread"
            )
        delivery = (
            QueuedDelivery(
                observer,
                capacity=capacity,  # type: ignore[arg-type]
                policy=policy,  # type: ignore[arg-type]
                name=f"{self.name}-{getattr(observer, 'name', 'observer')}",
            )
            if queued
            else None
        )
        entries = self._observer_entries()
        with self._registry_lock:
            if any(existing is observer for existing, _, _ in entries):
                return
            entries.append((observer, resolved, delivery))

    def remove_observer(self, observer: Observer) -> None:
        """Unsubscribe ``observer``. Silently ignores one that is not subscribed.

        A queued observer's delivery thread is drained and stopped here, so
        whatever it had already accepted still reaches it.
        """
        entries = self._observer_entries()
        delivery = None
        with self._registry_lock:
            for index, (existing, _, existing_delivery) in enumerate(entries):
                if existing is observer:
                    delivery = existing_delivery
                    del entries[index]
                    break
        if delivery is not None:
            delivery.close()

    @property
    def observers(self) -> Tuple[Observer, ...]:
        """Currently subscribed observers, in registration order."""
        return tuple(observer for observer, _, _ in self._observer_entries())

    def take_dropped(self) -> Tuple[int, Dict[str, int]]:
        """Return ``(total, {observer_name: count})`` of dropped observations,
        **zeroing the tally as it reads**.

        Pulled, not pushed. The owner calls this at a moment of its own choosing
        — typically a task boundary, on its own thread — and writes one log line.
        A per-drop callback would fire hardest exactly when the system is already
        struggling, on the producer's thread.

        Calling it is not optional in spirit: a bounded queue that drops without
        anyone reading the tally fails **invisibly**, which is worse than an
        unbounded one that fails loudly. Observations lost silently are only
        discovered offline, when the run cannot be repeated.
        """
        total = 0
        by_observer: Dict[str, int] = {}
        for observer, _, delivery in list(self._observer_entries()):
            if delivery is None:
                continue
            n = delivery.take_dropped()
            if n:
                name = getattr(observer, "name", observer.__class__.__name__)
                by_observer[name] = by_observer.get(name, 0) + n
                total += n
        return total, by_observer

    def close_observers(self, timeout: float = 2.0) -> bool:
        """Drain and stop every queued delivery. Returns ``False`` if any timed out.

        **Call this during teardown.** Anything a delivery thread still had
        queued disappears with the process otherwise — a real loss, unrecorded.
        Observers without their own queue need nothing here.
        """
        drained = True
        for _, _, delivery in list(self._observer_entries()):
            if delivery is not None and not delivery.close(timeout):
                drained = False
        return drained

    def set_slow_dispatcher(self, dispatcher: Optional[SlowDispatcher]) -> None:
        """Supply the hand-off used for ``SLOW`` observers.

        On this (pull) path the Sensor creates no thread of its own; it delegates.
        Assembly wires in ``run_async`` / ``run_background``.

        The broader claim "a Sensor never creates a thread" was retracted
        2026-07-27 (see the module docstring): EVO-012's continuous mode gives the
        Sensor an acquisition thread. Hand-off for SLOW observers stays as it is
        either way — a slow consumer must never sit on the device read.
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
        if not self.role:
            raise ValueError(
                f"sensor '{self.name}' has no role; assign one before observe() "
                "(e.g. sensor.role = 'nest'). The role is the device's position "
                "in the system, and observations are identified by it"
            )
        observation = self._acquire_one()
        self._publish(observation)
        return observation

    # -- continuous acquisition (EVO-012) ---------------------------------- #

    def start_streaming(self, *, min_interval: Optional[float] = None) -> None:
        """Start an acquisition thread that reads and publishes continuously.

        This is the mode that decouples acquisition from consumption: the thread
        reads at whatever rate the device sustains, and consumers keep up or drop
        under their own policy. Nothing downstream can slow it down — which is
        the entire point, and measurably so: with consumption in the loop pluck's
        burst path served 288 ms/frame against a 0.05-0.30 s motion window and
        caught 1-3 frames per lift; with it out, 41 ms/frame.

        What this thread is **not** allowed to do, and does not do — the rule that
        keeps ``architecture.md``'s single-scheduler invariant intact: **it writes
        no control state, sends no notes, and participates in no criteria.** It
        only produces observations and hands them to subscribers. The BT remains
        the sole *control* scheduler; that rule binds Workers, and a Sensor is not
        one.

        ``min_interval`` is an optional floor between reads, for devices whose
        read returns instantly and would otherwise spin a core (a mock, a cached
        continuous sensor). Leave it ``None`` for a camera: the blocking grab is
        the pacing. It is a throttle, **not** a schedule — there is no catch-up,
        no drift correction and no timer.

        Idempotent: calling it while already streaming does nothing.
        """
        if min_interval is not None and min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        role = self.role
        if not role:
            raise ValueError(
                f"sensor '{self.name}' has no role; assign one before streaming "
                "(e.g. sensor.role = 'nest')"
            )
        with self._stream_lock:
            thread = getattr(self, "_stream_thread", None)
            if thread is not None and thread.is_alive():
                return
            stop = threading.Event()
            self._stream_stop = stop
            thread = threading.Thread(
                target=self._stream_loop,
                args=(stop, min_interval),
                daemon=True,
                name=f"acquire-{role}",
            )
            self._stream_thread = thread
            thread.start()

    def stop_streaming(self, *, timeout: float = 2.0) -> bool:
        """Stop the acquisition thread and wait for it to finish.

        Returns ``False`` if it was still running when ``timeout`` elapsed —
        which means a device read is wedged, worth surfacing rather than
        pretending the stop succeeded. Idempotent.
        """
        with self._stream_lock:
            thread = getattr(self, "_stream_thread", None)
            stop = getattr(self, "_stream_stop", None)
            self._stream_thread = None
            self._stream_stop = None
        if stop is not None:
            stop.set()
        if thread is None:
            return True
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "sensor '%s': acquisition thread still running %.1fs after stop "
                "(a device read is likely blocked)", self.name, timeout,
            )
            return False
        return True

    @property
    def is_streaming(self) -> bool:
        """Whether an acquisition thread is currently running."""
        thread = getattr(self, "_stream_thread", None)
        return thread is not None and thread.is_alive()

    def _stream_loop(self, stop: threading.Event, min_interval: Optional[float]) -> None:
        """Read -> mint -> publish, until stopped.

        A failing read is logged and retried rather than ending the stream: a
        single dropped USB packet should not silently end acquisition halfway
        through a motion, leaving the caller to wonder why the frames stopped.
        """
        while not stop.is_set():
            started = time.monotonic()
            try:
                observation = self._acquire_one()
            except Exception:  # noqa: BLE001 - a bad read must not end the stream
                logger.exception(
                    "sensor '%s': acquisition read failed; continuing", self.name
                )
                observation = None
            if observation is not None:
                self._publish(observation)
            if min_interval:
                remaining = min_interval - (time.monotonic() - started)
                if remaining > 0:
                    stop.wait(remaining)

    def _acquire_one(self) -> Observation:
        """One read + mint, under the device lock. Shared by both modes."""
        role = self.role
        if not role:
            raise ValueError(f"sensor '{self.name}' has no role")
        with self._observe_lock:
            payload = self._read_payload()
            captured_at = time.monotonic()
            return self._build_observation(
                observation_id=self._next_observation_id(),
                source=role,
                captured_at=captured_at,
                payload=payload,
            )

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

    @property
    def _stream_lock(self) -> threading.Lock:
        """Guards acquisition-thread start/stop. Separate from
        :attr:`_observe_lock` so stopping a stream never has to wait behind the
        very read it is trying to stop."""
        return self._lazy_lock("_stream_lock_obj")

    def _observer_entries(
        self,
    ) -> List[Tuple[Observer, ObserverSpeed, Optional[QueuedDelivery]]]:
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
        isolation) — but it is logged, not swallowed.

        A consumer registered with a bounded queue is fed by a non-blocking
        ``offer`` — the producer's whole cost for it. Whether the offer was
        accepted is deliberately **not** acted on here: the drop is already on
        that delivery's tally, and the acquisition thread is the last place that
        should be branching on a consumer's backlog.
        """
        for observer, speed, delivery in list(self._observer_entries()):
            if delivery is not None:
                delivery.offer(observation)
                continue
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
