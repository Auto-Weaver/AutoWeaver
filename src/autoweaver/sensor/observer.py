"""Observer — the side a Sensor drives when it has observed something.

See EVO-011 §2.2 / §2.3. The drive chain is **BT -> Sensor -> Observer**: the BT
tree remains the system's only active **control** scheduler, and the Sensor is
*active* towards its observers — having produced an observation, it pushes.

RETRACTED 2026-07-27 — "no internal heartbeat, no thread"
---------------------------------------------------------
This paragraph originally continued: "...so the Sensor stays passive with respect
to the clock (*no internal heartbeat, no thread*)". **That clause no longer
holds.** It is kept here and marked rather than deleted: the argument was right
about Workers and wrong about devices, and that distinction is the whole point.

Why it fell: ``observe()`` is **pull**. The caller asks, one reading comes back,
so the rhythm is capture -> consume -> capture -> consume and the *acquisition
rate is bound by the consumer*. Handing SLOW observers off (see below) does not
fix it — a FAST observer's cost, plus whatever the caller itself does with the
frame, still sits in front of the next ``observe()``. A camera is natively
**push**: it emits at its own frame rate. Decoupling acquisition from consumption
therefore requires the Sensor to own an acquisition rhythm, i.e. a thread.

Measured, in pluck's burst path (``backend/src/workers/drill_vision.py:78-95``):
PNG encode ~247 ms against ``capture()`` ~31 ms — the consumer was **8x** the
producer, so the loop served 288 ms/frame into a 0.05-0.30 s motion window and
caught 1-3 frames per lift. That file's own conclusion: "调 lift_move_step_mm
**毫无用处**" — the trigger gate was never the limit, service time was. pluck
then grew a ``drill-lift-stream`` thread plus async writeback to get out of it,
so an acquisition thread is not new work: it is **moving a thread that already
exists in the business layer down to where it belongs**.

Where this docstring went wrong: it generalised ``architecture.md``'s "**no
Worker** may keep its own heartbeat" to the device layer. A Worker participates
in control — it takes notes, writes state, the BT reads criteria off it. A Sensor
does not. The load-bearing rule is narrower and survives intact: **an acquisition
thread writes no control state, sends no notes, and participates in no criteria**;
it only puts observations in the ring. The BT stays the sole *control* scheduler.

Consequences (designed in EVO-012, not here): both modes must exist — **on
demand** (pull, what ``observe()`` does today: stop, flush, hand back the
freshest frame) and **continuous** (push, acquisition at its own rhythm, slow
consumers dropping frames instead of throttling the device). Clock
synchronisation becomes a hard Sensor responsibility: acquisition threads run
independently, but ``captured_at`` must come from one clock or multi-camera
observations will not line up with the arm trajectory.

Preview, video recording, sample archiving, run logging, detection: **all of them
are Observers**. That is what decouples them from the number of devices. Adding a
second camera stops being a change to every consumer and becomes one more sensor
with its own subscribers.

Wiring observers is an **assembly** concern, not BT topology. Whether preview is
on or video is being recorded is not a branch in the business flow and has no
business appearing in the tree.

Fast or slow — you must say which
---------------------------------
The Sensor fans out on whatever thread called ``observe()``, which under the BT
is the tick thread. A fast observer (one ``imshow``) is fine there; a slow one
(video encoding, writing to disk) will stall the tick. The symptom of getting
this wrong is "the whole BT went choppy", and it is notoriously hard to pin on
any particular observer — so the speed is **declared, not guessed**.

A ``SLOW`` observer is never called inline. The Sensor hands it to a dispatcher
supplied by the assembly (``run_async`` / ``run_background`` in practice, see
``architecture.md``). Registering a slow observer with no dispatcher raises
immediately: a loud failure at wiring time beats a mysterious stall at runtime.

Holding on to an observation
----------------------------
A slow observer may keep the ``Observation`` it was handed for as long as it
needs; in this cut the payload lives as long as the reference does. When bounded
storage lands (EVO-011 §5.4) expiry must have an **explicit outcome** — the
consumer either gets the data or is told plainly that it is gone. Never a stale
read.

That ring is no longer a later storage optimisation. Once acquisition runs on its
own thread (see the retraction above) the ring is **where produced observations
go**, so bounded storage becomes a prerequisite for continuous mode rather than a
memory tidy-up. Sizing, drop policy and expiry semantics: EVO-012.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from autoweaver.sensor.observation import Observation


class ObserverSpeed(Enum):
    """Where an observer is allowed to run.

    ``FAST``: cheap enough to run inline on the caller's (tick) thread.
    ``SLOW``: must be handed off; the Sensor refuses to call it inline.
    """

    FAST = "fast"
    SLOW = "slow"


class Observer(ABC):
    """A consumer a Sensor pushes observations to.

    Implementations declare :attr:`speed` — there is deliberately no default,
    because the safe-looking default (``FAST``) is exactly the one that stalls
    the tick when it is wrong.
    """

    @property
    @abstractmethod
    def speed(self) -> ObserverSpeed:
        """Whether this observer may run inline on the caller's thread."""

    @property
    def name(self) -> str:
        """Identifier used in logs — notably when this observer raises."""
        return self.__class__.__name__

    @abstractmethod
    def on_observation(self, observation: Observation) -> None:
        """Handle one observation.

        Raising is isolated by the Sensor (logged, other observers still run) —
        a crashing preview must not take perception down with it. It is still a
        bug; the Sensor does not silence it.
        """


__all__ = ["Observer", "ObserverSpeed"]
