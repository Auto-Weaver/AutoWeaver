"""The `Geometry` runtime container.

Holds the two classes of static transforms (`world ← arm_i_base` and
`arm_i_flange ← arm_i_tool_X`) plus their pre-computed inverses. One
instance per process, owned by motion_policy.

This module knows nothing about robot SDKs, WorldBoard, or BT nodes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from autoweaver.geometry import schema, transforms

_BASE_PATTERN = re.compile(r"^arm_[a-z0-9_]+_base$")
_TOOL_PATTERN = re.compile(r"^arm_[a-z0-9_]+_tool_[a-z0-9_]+$")
_FIXTURE_PATTERN = re.compile(r"^fixture_[a-z0-9_]+$")
_FLANGE_PATTERN = re.compile(r"^arm_[a-z0-9_]+_flange$")

logger = logging.getLogger(__name__)


class Geometry:
    """Calibration data, indexed for O(1) lookup at runtime.

    Two categories of static transforms:
      - `world ← <frame>` for arm bases and fixtures (`parent: world`)
      - `<flange> ← <frame>` for tools (`parent: arm_<id>_flange`)

    Inverses are pre-computed at construction. Runtime is pure dict lookup.
    """

    def __init__(self, calibration_path: str | Path) -> None:
        edges = schema.load(calibration_path)

        self._world_from: dict[str, np.ndarray] = {}
        self._from_world: dict[str, np.ndarray] = {}
        self._flange_from: dict[str, np.ndarray] = {}
        self._from_flange: dict[str, np.ndarray] = {}
        # Track which flange each tool hangs off, for the dynamic-side composer.
        self._tool_to_flange: dict[str, str] = {}

        for edge in edges:
            if edge.parent == "world":
                self._world_from[edge.name] = edge.matrix
                self._from_world[edge.name] = transforms.invert(edge.matrix)
            elif _FLANGE_PATTERN.match(edge.parent):
                self._flange_from[edge.name] = edge.matrix
                self._from_flange[edge.name] = transforms.invert(edge.matrix)
                self._tool_to_flange[edge.name] = edge.parent
            else:
                # schema.load already rejects this, but stay defensive.
                raise ValueError(f"unexpected parent {edge.parent!r} for {edge.name!r}")

        logger.info("geometry: loaded %d frames from %s", len(edges), calibration_path)
        self._log_tree(calibration_path)

    # ─── Public API ─────────────────────────────────────────────────────

    def world_from(self, name: str) -> np.ndarray:
        """Return T(world ← name). `name` must be an arm base or fixture."""
        try:
            return self._world_from[name]
        except KeyError:
            raise KeyError(self._explain_world_lookup(name)) from None

    def base_from_world(self, name: str) -> np.ndarray:
        """Return inv(T(world ← name)) = T(name ← world)."""
        try:
            return self._from_world[name]
        except KeyError:
            raise KeyError(self._explain_world_lookup(name)) from None

    def flange_from(self, name: str) -> np.ndarray:
        """Return T(flange ← name). `name` must be a tool."""
        try:
            return self._flange_from[name]
        except KeyError:
            raise KeyError(self._explain_flange_lookup(name)) from None

    def tool_from_flange(self, name: str) -> np.ndarray:
        """Return inv(T(flange ← name)) = T(name ← flange)."""
        try:
            return self._from_flange[name]
        except KeyError:
            raise KeyError(self._explain_flange_lookup(name)) from None

    def flange_of(self, tool: str) -> str:
        """Return the flange name a given tool hangs off (e.g. 'arm_1_flange')."""
        try:
            return self._tool_to_flange[tool]
        except KeyError:
            raise KeyError(self._explain_flange_lookup(tool)) from None

    # ─── Internals ──────────────────────────────────────────────────────

    def _explain_world_lookup(self, name: str) -> str:
        if name in self._flange_from:
            return (
                f"{name!r} is a tool (hangs off a flange), not a world-relative frame; "
                "use flange_from() / tool_from_flange() instead"
            )
        if _FLANGE_PATTERN.match(name):
            return (
                f"{name!r} is a dynamic flange frame; read its pose from the arm SDK, "
                "not from geometry"
            )
        return (
            f"frame {name!r} not found among world-relative frames; "
            f"loaded: {sorted(self._world_from)}"
        )

    def _explain_flange_lookup(self, name: str) -> str:
        if name in self._world_from:
            return (
                f"{name!r} is a world-relative frame, not a tool; "
                "use world_from() / base_from_world() instead"
            )
        return (
            f"tool {name!r} not found among flange-relative frames; "
            f"loaded: {sorted(self._flange_from)}"
        )

    def _log_tree(self, path: str | Path) -> None:
        """Print the frame topology at INFO level so a human can sanity-check
        what was loaded by glancing at startup logs."""
        # Group tools under their flange, group flanges under their arm base.
        bases = sorted(n for n in self._world_from if _BASE_PATTERN.match(n))
        fixtures = sorted(n for n in self._world_from if _FIXTURE_PATTERN.match(n))
        tools_by_flange: dict[str, list[str]] = {}
        for tool, flange in self._tool_to_flange.items():
            tools_by_flange.setdefault(flange, []).append(tool)

        lines = [f"geometry: frame topology loaded from {path}", "world"]
        all_top = bases + fixtures
        for i, name in enumerate(all_top):
            is_last_top = i == len(all_top) - 1
            top_prefix = "└── " if is_last_top else "├── "
            lines.append(f"{top_prefix}{name}")
            if _BASE_PATTERN.match(name):
                # bases own a flange; flanges may own tools.
                arm_id = name[len("arm_") : -len("_base")]
                flange_name = f"arm_{arm_id}_flange"
                child_indent = "    " if is_last_top else "│   "
                lines.append(f"{child_indent}└── {flange_name} (dynamic, from SDK)")
                tools = sorted(tools_by_flange.get(flange_name, []))
                for j, tool in enumerate(tools):
                    is_last_tool = j == len(tools) - 1
                    tool_prefix = "└── " if is_last_tool else "├── "
                    tools_indent = child_indent + "    "
                    lines.append(f"{tools_indent}{tool_prefix}{tool}")

        # Tools attached to flanges whose base is missing — schema doesn't enforce
        # this; flag it visibly so the user spots the dangling calibration.
        known_flanges = {f"arm_{n[len('arm_') : -len('_base')]}_flange" for n in bases}
        dangling = [f for f in tools_by_flange if f not in known_flanges]
        for flange in sorted(dangling):
            lines.append(f"⚠ {flange} (no matching arm_<id>_base calibration)")
            for tool in sorted(tools_by_flange[flange]):
                lines.append(f"    └── {tool}")

        logger.info("\n".join(lines))
