from __future__ import annotations

from typing import Any


class Blackboard:
    """Shared key-value store for BT nodes.

    Single-writer rule: each key can only be written by one designated node.
    Any node can read any key. No locks needed (single-threaded tick).
    """

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._writers: dict[str, str] = {}
        self._types: dict[str, type] = {}

    def register_key(self, key: str, value_type: type, writer: str) -> None:
        """Register a key with its type and writer. Called during tree construction."""
        if key in self._writers and self._writers[key] != writer:
            raise ValueError(
                f"Key '{key}' already owned by '{self._writers[key]}', "
                f"cannot assign to '{writer}'"
            )
        self._writers[key] = writer
        self._types[key] = value_type

    def write(self, key: str, value: Any, writer: str) -> None:
        """Write a value. Only the registered writer can write."""
        registered = self._writers.get(key)
        if registered is not None and registered != writer:
            raise PermissionError(
                f"'{writer}' has no write access to '{key}' (owned by '{registered}')"
            )
        expected_type = self._types.get(key)
        if expected_type is not None and not isinstance(value, expected_type):
            raise TypeError(
                f"Key '{key}' expects {expected_type.__name__}, got {type(value).__name__}"
            )
        self._data[key] = value

    def read(self, key: str, default: Any = None) -> Any:
        """Read a value. Any node can read any key."""
        return self._data.get(key, default)

    def set_initial(self, key: str, value: Any) -> None:
        """Set an initial value before the tree starts ticking.

        Initial values bypass writer checks — they come from outside the tree
        (perception results, user config, process parameters).
        """
        self._data[key] = value

    def has_key(self, key: str) -> bool:
        return key in self._data

    def keys(self) -> list[str]:
        return list(self._data.keys())

    def clear(self) -> None:
        """Clear all data but keep registrations."""
        self._data.clear()

    def reset(self) -> None:
        """Full reset: clear data and registrations."""
        self._data.clear()
        self._writers.clear()
        self._types.clear()
