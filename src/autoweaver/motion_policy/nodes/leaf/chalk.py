"""Chalk — blackboard bookkeeping leaf.

"Chalk can only write on the blackboard." A single-tick leaf whose only
side effect is writing one blackboard key; it always SUCCEEDs, never goes
RUNNING. The name is the permission boundary: chalk writes the blackboard
and nothing else — every other side effect must go through a note to a
Worker (EVO-007).

All Chalk instances share the writer identity ``"chalk"``, so several may
write the same key (idempotent same-writer registration), while any
non-chalk writer touching a chalk key is rejected with PermissionError.
Never uses ``set_initial`` — writes go through the ``write`` front door.

See EVO-010.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from autoweaver.motion_policy.nodes.node import Status, TreeNode

if TYPE_CHECKING:
    from autoweaver.motion_policy.world_board import Snapshot


class Chalk(TreeNode):
    """Write one blackboard key from ``fn(snapshot, current) -> new_value``.

    The general form reads the snapshot and the current value (``None`` if
    the key is unset) and computes the new value in one step — enough for
    find-or-insert style bookkeeping. Named constructors cover the common
    cases:

    - ``Chalk.inc(key, by=1)`` — current (default 0) + by.
    - ``Chalk.set(key, value)`` — value is a constant or ``f(snapshot)``.
    - ``Chalk.append(key, value)`` — treat current as a list (default [])
      and append value (constant or ``f(snapshot)``).
    """

    WRITER = "chalk"

    def __init__(
        self,
        key: str,
        fn: Callable[["Snapshot", Any], Any],
        name: str = "",
    ):
        super().__init__(name=name or f"Chalk({key})")
        self._key = key
        self._fn = fn

    def set_blackboard(self, blackboard, key_mapping=None) -> None:
        super().set_blackboard(blackboard, key_mapping)
        blackboard.register_key(self._key, object, self.WRITER)

    def on_start(self) -> Status:
        current = self._blackboard.read(self._key)
        new_value = self._fn(self._snapshot, current)
        self._blackboard.write(self._key, new_value, self.WRITER)
        return Status.SUCCESS

    def on_running(self) -> Status:
        # Chalk is single-tick — should never reach RUNNING.
        return Status.SUCCESS

    @classmethod
    def inc(cls, key: str, by: Any = 1, name: str = "") -> "Chalk":
        def fn(snapshot: Any, current: Any) -> Any:
            return (current if current is not None else 0) + by

        return cls(key, fn, name=name or f"Chalk.inc({key})")

    @classmethod
    def set(cls, key: str, value: Any, name: str = "") -> "Chalk":
        def fn(snapshot: Any, current: Any) -> Any:
            return value(snapshot) if callable(value) else value

        return cls(key, fn, name=name or f"Chalk.set({key})")

    @classmethod
    def append(cls, key: str, value: Any, name: str = "") -> "Chalk":
        def fn(snapshot: Any, current: Any) -> Any:
            items = list(current) if current is not None else []
            items.append(value(snapshot) if callable(value) else value)
            return items

        return cls(key, fn, name=name or f"Chalk.append({key})")
