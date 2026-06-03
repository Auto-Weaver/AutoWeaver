"""YAML schema parser and validator for calibration files.

Produces a list of `FrameEdge` dataclasses. A frame entry is one of two
kinds:

  - **static** — a fixed calibrated transform. Rotation given as
    `rpy: [rx, ry, rz]` (Euler, ZYX intrinsic, degrees — the canonical
    vendor teach-pendant convention) or `matrix: [[...]]` (4×4 homogeneous,
    an escape hatch). Translation `xyz` is always in mm.
  - **dynamic** — a live edge whose 4×4 is *not* stored here but read at
    runtime from a WorldBoard snapshot. Declared with a `dynamic:` block
    carrying `state_key` (the snapshot key a Worker publishes to) and
    `required` (whether a missing value is fatal or treated as identity).

Frame names and parents are **not** constrained to any naming convention —
declare whatever topology the cell needs. The validator only enforces
structural integrity (unique names, known fields, well-formed matrices,
exactly one of the mutually-exclusive rotation/dynamic forms) so that a
malformed file fails loud at load instead of silently mis-placing a frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from autoweaver.frames import transforms

_ROTATION_FIELDS = ("rpy", "matrix")
_KNOWN_FIELDS = frozenset({"name", "parent", "xyz", "rpy", "matrix", "dynamic"})
_KNOWN_DYNAMIC_FIELDS = frozenset({"state_key", "required"})

_RPY_CONVENTION = "zyx_intrinsic_deg"


@dataclass(frozen=True)
class FrameEdge:
    """One frame edge: `parent ← name`.

    Static edge: `matrix` is the fixed 4×4 homogeneous transform (mm), and
    `state_key` is None.

    Dynamic edge: `matrix` is None and the transform is read at runtime from
    ``snapshot[state_key]``; `required` says whether a missing value is fatal
    (True) or treated as identity (False).
    """

    name: str
    parent: str
    matrix: np.ndarray | None = None
    state_key: str | None = None
    required: bool = False

    @property
    def is_dynamic(self) -> bool:
        return self.state_key is not None



class CalibrationSchemaError(ValueError):
    """Raised when the calibration YAML violates the schema."""


def load(path: str | Path) -> list[FrameEdge]:
    """Parse and validate a calibration YAML, returning standardized edges."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise CalibrationSchemaError(f"failed to parse {path}: {e}") from e

    if not isinstance(raw, dict) or "frames" not in raw:
        raise CalibrationSchemaError(f"{path}: top level must be a mapping with 'frames' key")
    frames_raw = raw["frames"]
    if not isinstance(frames_raw, list):
        raise CalibrationSchemaError(f"{path}: 'frames' must be a list")

    edges: list[FrameEdge] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(frames_raw):
        if not isinstance(entry, dict):
            raise CalibrationSchemaError(f"{path}: frames[{i}] must be a mapping")
        try:
            edge = _parse_entry(entry)
        except (CalibrationSchemaError, ValueError) as e:
            name_hint = entry.get("name", f"<index {i}>")
            raise CalibrationSchemaError(f"{path}: frame {name_hint!r}: {e}") from None
        if edge.name in seen_names:
            raise CalibrationSchemaError(f"{path}: duplicate frame name {edge.name!r}")
        seen_names.add(edge.name)
        edges.append(edge)

    return edges


def _parse_entry(entry: dict[str, Any]) -> FrameEdge:
    unknown = set(entry.keys()) - _KNOWN_FIELDS
    if unknown:
        raise CalibrationSchemaError(f"unknown fields: {sorted(unknown)}")

    name = entry.get("name")
    parent = entry.get("parent")
    if not isinstance(name, str) or not isinstance(parent, str):
        raise CalibrationSchemaError("'name' and 'parent' must be strings")
    if not name or not parent:
        raise CalibrationSchemaError("'name' and 'parent' must be non-empty")

    is_dynamic = "dynamic" in entry
    has_static = any(f in entry for f in (*_ROTATION_FIELDS, "xyz"))
    if is_dynamic and has_static:
        raise CalibrationSchemaError(
            "a dynamic edge must not also carry 'xyz' / 'rpy' / 'matrix'; "
            "its transform comes from the snapshot at runtime"
        )

    if is_dynamic:
        state_key, required = _parse_dynamic(entry["dynamic"])
        return FrameEdge(
            name=name, parent=parent, matrix=None,
            state_key=state_key, required=required,
        )

    xyz_raw = entry.get("xyz")
    if xyz_raw is None and "matrix" not in entry:
        raise CalibrationSchemaError(
            "must provide 'xyz' (+ 'rpy'), 'matrix', or a 'dynamic' block"
        )
    matrix = _build_matrix(entry, xyz_raw)
    return FrameEdge(name=name, parent=parent, matrix=matrix)


def _parse_dynamic(block: Any) -> tuple[str, bool]:
    if not isinstance(block, dict):
        raise CalibrationSchemaError("'dynamic' must be a mapping")
    unknown = set(block.keys()) - _KNOWN_DYNAMIC_FIELDS
    if unknown:
        raise CalibrationSchemaError(f"unknown 'dynamic' fields: {sorted(unknown)}")
    state_key = block.get("state_key")
    if not isinstance(state_key, str) or not state_key:
        raise CalibrationSchemaError("'dynamic.state_key' must be a non-empty string")
    required = block.get("required", False)
    if not isinstance(required, bool):
        raise CalibrationSchemaError("'dynamic.required' must be a boolean")
    return state_key, required


def _build_matrix(entry: dict[str, Any], xyz_raw: Any) -> np.ndarray:
    present = [f for f in _ROTATION_FIELDS if f in entry]
    if len(present) == 0:
        raise CalibrationSchemaError(
            f"must provide one of {_ROTATION_FIELDS} for the rotation part"
        )
    if len(present) > 1:
        raise CalibrationSchemaError(
            f"rotation fields are mutually exclusive, got: {present}"
        )
    rotation_field = present[0]

    if rotation_field == "matrix":
        if xyz_raw is not None:
            raise CalibrationSchemaError(
                "'matrix' is self-contained; do not combine with 'xyz'"
            )
        return transforms.matrix_passthrough(entry["matrix"])

    # rotation_field == "rpy"
    xyz_mm = transforms.to_mm(xyz_raw, "mm")
    return transforms.euler_to_matrix(xyz_mm, entry["rpy"], _RPY_CONVENTION)
