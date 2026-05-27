"""NotifyLeaf — pass a note to a Worker and immediately return SUCCESS.

Fire-and-forget: the leaf does not wait for the Worker to act on the
note. Use a separate WaitFor downstream to observe the result via state.
See EVO-005 / EVO-007.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from autoweaver.motion_policy.nodes.node import Status, TreeNode

if TYPE_CHECKING:
    from autoweaver.motion_policy.world_board import WorldBoard


class NotifyLeaf(TreeNode):
    """Pass a note to a target Worker and SUCCEED immediately.

    Example::

        NotifyLeaf(world_board, "perception", "start_picking", {"region": 3})

    The leaf calls ``world_board.pass_note(target, note_name, payload, sender)``
    in ``on_start`` and returns SUCCESS. It never observes whether the
    Worker actually acts on the note — that's a job for a downstream
    WaitFor (or other Condition) reading the Worker's state.

    The leaf is stateless across ticks; once it returns SUCCESS the BT's
    parent control node decides what to do next.
    """

    def __init__(
        self,
        world_board: WorldBoard,
        target: str,
        note_name: str,
        payload: Any | None = None,
        sender: str | None = None,
        name: str = "",
    ):
        super().__init__(name=name or f"Notify({target}.{note_name})")
        self._world_board = world_board
        self._target = target
        self._note_name = note_name
        self._payload = payload if payload is not None else {}
        self._sender = sender or self.name

    def on_start(self) -> Status:
        self._world_board.pass_note(
            namespace=self._target,
            name=self._note_name,
            payload=self._payload,
            sender=self._sender,
        )
        return Status.SUCCESS

    def on_running(self) -> Status:
        # NotifyLeaf is single-tick — should never reach RUNNING.
        return Status.SUCCESS
