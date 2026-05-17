"""NotifyAndWait — pass a note with auto-allocated request_id, wait for completion.

This leaf is the **standard pattern** for BT → Worker async commands. It
allocates a fresh ``request_id`` on ``on_start``, injects it into the
note payload as ``__request_id__``, dispatches via
``world_board.pass_note``, then returns RUNNING until the target
Worker's ``<target>.last_completed_id`` catches up to that request id.

The Worker side cooperates with this protocol by writing
``last_completed_id`` when the work for a given request_id finishes
(for async commands like motion, this typically happens on a state
edge, not synchronously in the note handler).

Compared to ``NotifyLeaf`` (fire-and-forget) + ``WaitFor`` (predicate
on state), ``NotifyAndWait``:

- Eliminates the race where two consecutive motion commands could be
  confused by a single ``done`` state edge — every request has its own
  monotonic id, and the leaf only succeeds when *its* id completes.
- Auto-injects the id, so the Worker's note handler can pull it back
  out of the payload without the BT having to thread it manually.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Callable, Union

from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.worker.base import next_request_id

if TYPE_CHECKING:
    from autoweaver.motion_policy.blackboard import Blackboard
    from autoweaver.motion_policy.world_board import Snapshot, WorldBoard


PayloadSpec = Union[
    dict,
    Callable[["Blackboard"], dict],
    Callable[["Blackboard", "Snapshot"], dict],
    None,
]


class NotifyAndWait(TreeNode):
    """Pass a note with auto-allocated request_id and wait for completion.

    Payload forms:

    - ``dict`` — sent as-is, plus the injected ``__request_id__``.
    - ``f(blackboard) -> dict`` — 1-arg callable. Computes payload from
      current blackboard state (e.g. building a move_l target from
      ``bb.x/y/z``).
    - ``f(blackboard, snapshot) -> dict`` — 2-arg callable. Also receives
      the WorldBoard snapshot, so payload can reference live Worker state
      (e.g. read ``<arm>.pose`` to carry rxryrz into a move target
      without the BT having to think about Euler unwrap).

    Signature is detected via ``inspect``. The 1-arg form stays the
    default for backwards compatibility with existing trees.

    Returns RUNNING until ``<target>.last_completed_id`` reaches the
    request_id allocated in ``on_start``. The request_id is reset on
    every ``reset()`` so the leaf is re-runnable inside loops.
    """

    def __init__(
        self,
        world_board: "WorldBoard",
        target: str,
        note_name: str,
        payload: PayloadSpec = None,
        sender: str | None = None,
        name: str = "",
    ):
        super().__init__(name=name or f"NotifyAndWait({target}.{note_name})")
        self._wb = world_board
        self._target = target
        self._note_name = note_name
        self._payload_spec = payload
        self._sender = sender or self.name
        self._rid: int = 0

    def on_start(self) -> Status:
        self._rid = next_request_id()
        payload = self._build_payload()
        payload["__request_id__"] = self._rid
        self._wb.pass_note(
            namespace=self._target,
            name=self._note_name,
            payload=payload,
            sender=self._sender,
        )
        return self._evaluate()

    def on_running(self) -> Status:
        return self._evaluate()

    def reset(self) -> None:
        self._rid = 0
        super().reset()

    def _build_payload(self) -> dict:
        if self._payload_spec is None:
            return {}
        if callable(self._payload_spec):
            try:
                sig = inspect.signature(self._payload_spec)
                nparams = len(sig.parameters)
            except (TypeError, ValueError):
                nparams = 1
            if nparams >= 2:
                return dict(self._payload_spec(self._blackboard, self.snapshot))
            return dict(self._payload_spec(self._blackboard))
        return dict(self._payload_spec)

    def _evaluate(self) -> Status:
        last_completed = self.snapshot.get(f"{self._target}.last_completed_id")
        if last_completed is None:
            last_completed = 0
        if int(last_completed) >= self._rid:
            return Status.SUCCESS
        return Status.RUNNING


class WaitForAdvance(TreeNode):
    """Wait for a WorldBoard counter to advance past a blackboard threshold.

    Reads ``<state_key>`` from the snapshot each tick and compares it
    against ``<bb_key>`` in the blackboard. Succeeds the first tick the
    state value strictly exceeds the threshold, and on success copies
    the new value into the blackboard so subsequent ticks see the
    advanced threshold.

    On first run (bb_key not yet set), the threshold is treated as 0.
    Useful for "wait for the operator to press an advance key" style
    handshakes where the producer increments a counter.
    """

    def __init__(self, state_key: str, bb_key: str, name: str = ""):
        super().__init__(name=name or f"WaitForAdvance({state_key})")
        self._state_key = state_key
        self._bb_key = bb_key

    def on_start(self) -> Status:
        return self._evaluate()

    def on_running(self) -> Status:
        return self._evaluate()

    def _evaluate(self) -> Status:
        current_raw = self.snapshot.get(self._state_key)
        current = int(current_raw) if current_raw is not None else 0
        threshold = self._read_threshold()
        if current > threshold:
            self._blackboard.write(self._bb_key, current, self.name)
            return Status.SUCCESS
        return Status.RUNNING

    def _read_threshold(self) -> int:
        value = self._blackboard.read(self._bb_key, 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
