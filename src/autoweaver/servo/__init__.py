"""Visual servoing — the closed-loop alignment layer. See NEXT-013.

A *sibling* feedback-control layer to Frames, not an extension of it. The
image Jacobian (interaction matrix) is not an SE(3) rigid transform — it
may be non-square, needs a pseudo-inverse, and changes with configuration
— so it cannot live inside the Frames graph (whose invariant is "every
edge is a closed-form-invertible rigid transform"). Instead servoing hangs
off the existing BTClock / WorldBoard / Worker stack as its own layer.

This package holds the **loop body** — the self-contained, hardware-free
core that has clear correctness and is testable on synthetic data:

  - ``InteractionMatrix`` provider protocol + a constant-2x2 implementation
    (the telecentric-lens, XY-only case that collapses the depth term).
  - ``ibvs_velocity`` — the control law ``v = -lambda * J^+ * e`` with gain,
    pseudo-inverse, and step clamping.
  - ``ServoState`` / convergence + divergence classification.

What this package deliberately does NOT own (the "seams" left soft per
NEXT-013 §2): how the alignment intent is defined (which feature point,
which offset), how the Jacobian is obtained for the real cross-arm case
(calibration vs online Broyden estimation), and the arm's servo command
mode (look-then-move vs in-motion correction). Those are wired at the
leaf / Worker layer, not here.
"""

from autoweaver.servo.controller import (
    ServoController,
    ServoDecision,
    ServoOutcome,
)
from autoweaver.servo.interaction import (
    ConstantInteractionMatrix,
    InteractionMatrix,
)
from autoweaver.servo.law import (
    ServoStep,
    ibvs_velocity,
)

__all__ = [
    "InteractionMatrix",
    "ConstantInteractionMatrix",
    "ibvs_velocity",
    "ServoStep",
    "ServoController",
    "ServoDecision",
    "ServoOutcome",
]
