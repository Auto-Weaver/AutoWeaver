"""Observer — the side a Sensor drives when it has observed something.

See EVO-011 §2.2 / §2.3. The drive chain is **BT -> Sensor -> Observer**: the BT
tree remains the system's only active scheduler, so the Sensor stays passive with
respect to the clock (no internal heartbeat, no thread) while being *active*
towards its observers — when it produces an observation, it pushes.

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
