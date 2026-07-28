"""Observation — one sensor's sampling of the world, with its identity attached.

See EVO-011. The short version: a reading is not a bare value, it is
**"who, when, under what conditions, through which optical model, sampled the
world"**. Before this module a reading left the production line anonymous
(``Sensor.snapshot() -> Any``), so every consumer re-derived — or guessed — the
context it needed, and every project invented its own way to carry it.

Why not ``Frame``
-----------------
``Frame`` is already a **coordinate frame** in this project (EVO-008: the Frames
graph, SE(3) rigid transforms, dynamic edges). An observation *carries* a
coordinate frame, so the two words would appear in the same sentence. Hence
``Observation`` — which also buys the multi-modality opening for free: a pressure
reading is an observation too, a camera's observation merely happens to carry
pixels and a projection model.

Identity is the shutter, not the object
---------------------------------------
``id`` / ``source`` / ``captured_at`` identify **the sampling event**. A crop of
an observation is a different *view* of the same event, so it inherits all three
unchanged. That is what makes ``id`` usable as a freshness gate (NEXT-013 §5:
"only act on a new frame, hold the previous command on a stale one") — the gate
asks "is this a new shutter?", not "is this a new Python object?".

Lineage
-------
Crop / resize produce a **new** Observation that remembers where it came from.
This is the structural defence against a whole bug family: once you crop to an
ROI, pixel coordinates silently change meaning, and today it is the business
code's job to remember to add the origin back. With ``derived_from`` the derived
observation can convert its own coordinates back itself — see :meth:`to_root`.

Payload ownership
-----------------
Constructing an Observation **takes ownership of the payload**. For ``ndarray``
payloads the array is marked read-only, so "immutable" is enforced rather than
merely documented. A Sensor must therefore not hand over a buffer it intends to
reuse, and an Observer that wants to draw on the pixels must derive its own copy
(which is correct anyway — an overlay is a derived image, not the observation).

Payload lifetime **in this cut**: an Observation holds a strong reference to its
payload, so a slow consumer that holds the Observation keeps the data alive.
There is no bounded ring and nothing expires. EVO-011 §5.4 leaves the bounded
storage design open — when it lands, expiry **must** have an explicit outcome
(you get the data, or you are explicitly told you cannot). It must never become
a stale read.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, Tuple

try:  # numpy is a hard dependency in practice; stay import-safe regardless
    import numpy as np
except Exception:  # pragma: no cover - numpy is always present in this project
    np = None  # type: ignore[assignment]


@dataclass(frozen=True)
class PixelTransform:
    """Affine map from a derived observation's pixels back to its parent's.

    The convention is **parent = local * scale + offset**, applied per axis.
    Both derivations this cut supports fall out of it:

      - crop at ``(x0, y0)``     -> ``scale = 1``,     ``offset = (x0, y0)``
      - resize by ``(sx, sy)``   -> ``scale = 1/s``,   ``offset = 0``

    Keeping one form for both is what makes chains composable: walking up a
    lineage is just applying each transform in turn (:meth:`Observation.to_root`).
    """

    scale_x: float = 1.0
    scale_y: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    def to_parent(self, u: float, v: float) -> Tuple[float, float]:
        """Map a local pixel coordinate to the parent's coordinate system."""
        return (u * self.scale_x + self.offset_x, v * self.scale_y + self.offset_y)

    @classmethod
    def crop(cls, x0: float, y0: float) -> "PixelTransform":
        """Transform for a crop whose top-left corner sits at ``(x0, y0)``."""
        return cls(1.0, 1.0, float(x0), float(y0))

    @classmethod
    def resize(cls, scale_x: float, scale_y: float) -> "PixelTransform":
        """Transform for a resize by ``scale_x`` / ``scale_y`` (new = old * s)."""
        if scale_x == 0 or scale_y == 0:
            raise ValueError("resize scale must be non-zero")
        return cls(1.0 / float(scale_x), 1.0 / float(scale_y), 0.0, 0.0)


@dataclass(frozen=True)
class Derivation:
    """How one Observation came from another. ``None`` on a root observation."""

    parent: "Observation"
    kind: str
    transform: PixelTransform


def _take_ownership(payload: Any) -> Any:
    """Mark an ndarray payload read-only. Non-array payloads pass through.

    Going writeable True -> False is always permitted by numpy, including on a
    view, so this never raises for a well-formed array.
    """
    if np is not None and isinstance(payload, np.ndarray):
        try:
            payload.flags.writeable = False
        except (ValueError, AttributeError):  # pragma: no cover - defensive
            pass
    return payload


@dataclass(frozen=True)
class Observation:
    """One sensor's sampling of the world.

    Attributes:
        id: Monotonic per ``source``. Identifies the **sampling event**; a
            derived view inherits it unchanged.
        source: The sensor's *role* — its position in the system (``"nest"`` /
            ``"drill"``), never a device model name. Two Daheng cameras are not
            both ``"DahengCamera"``.
        captured_at: Monotonic clock at acquisition. Only the Sensor knows it;
            anything stamped later is "when the consumer got round to it".
        data: The payload. Owned by this Observation and treated as immutable.
        conditions: Imaging / acquisition conditions the device alone knows
            (exposure, gain, white balance...). This is what today survives only
            as a YAML comment saying "if you change the exposure, go re-check the
            detection threshold".
        projection: **Reference** to this source's optical / calibration model.
            Deliberately opaque here — interpretation belongs to the business
            layer, exactly as ``logbook`` treats board values.
        derived_from: Lineage. ``None`` on a root observation.

    Not in here, on purpose:
        - **detections** — an observation is the raw record of what was seen;
          detections are *inferred* from it by later steps.
        - **the device object** — lifetime belongs to the Sensor.
        - **the pose at capture time** — would require a ``Scribe``/``Transcript``,
          cut from this round (EVO-011 §5.1). Pixel->world *position* therefore
          still needs an externally supplied pose, exactly as today. ``projection``
          alone still answers pixel->millimetre *scale*, which is what the
          scattered per-lens scale constants actually need.
    """

    id: int
    source: str
    captured_at: float
    data: Any
    conditions: Mapping[str, Any] = field(default_factory=dict)
    projection: Any = None
    derived_from: Optional[Derivation] = None

    def __post_init__(self) -> None:
        _take_ownership(self.data)

    # -- lineage ----------------------------------------------------------- #

    @property
    def is_root(self) -> bool:
        """True when this observation came straight off the sensor."""
        return self.derived_from is None

    @property
    def root(self) -> "Observation":
        """The original observation this one was derived from (self if root)."""
        node = self
        while node.derived_from is not None:
            node = node.derived_from.parent
        return node

    def lineage(self) -> Tuple[Derivation, ...]:
        """Derivations from this observation up to the root, nearest first."""
        chain = []
        node = self
        while node.derived_from is not None:
            chain.append(node.derived_from)
            node = node.derived_from.parent
        return tuple(chain)

    def to_parent(self, u: float, v: float) -> Tuple[float, float]:
        """Map a local pixel coordinate into the parent's coordinate system.

        Raises ``ValueError`` on a root observation — it has no parent, and
        silently returning ``(u, v)`` would hide a caller's mistake.
        """
        if self.derived_from is None:
            raise ValueError("root observation has no parent")
        return self.derived_from.transform.to_parent(u, v)

    def to_root(self, u: float, v: float) -> Tuple[float, float]:
        """Map a local pixel coordinate all the way back to the root's frame.

        This is the point of lineage: a coordinate measured inside a cropped
        observation converts itself back, so business code never has to remember
        to add the crop origin.
        """
        x, y = float(u), float(v)
        node = self
        while node.derived_from is not None:
            x, y = node.derived_from.transform.to_parent(x, y)
            node = node.derived_from.parent
        return (x, y)

    # -- derivation -------------------------------------------------------- #

    def derive(self, data: Any, *, kind: str, transform: PixelTransform) -> "Observation":
        """Build a child observation of the same *kind* as ``self``.

        Identity (``id`` / ``source`` / ``captured_at``) is inherited: a derived
        view is the same sampling event seen differently. ``conditions`` and
        ``projection`` carry over because neither is changed by reframing —
        the exposure that produced these pixels is still the exposure that
        produced them.
        """
        return replace(
            self,
            data=data,
            derived_from=Derivation(parent=self, kind=kind, transform=transform),
        )


__all__ = ["Derivation", "Observation", "PixelTransform"]
