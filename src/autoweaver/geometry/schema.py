"""YAML schema parser and validator for calibration files.

Produces a list of `FrameEdge` dataclasses with translation in mm and
rotation expressed as a standard 4×4 matrix. Downstream code (Geometry)
sees only the standardized form.

The schema accepts two rotation representations:
  - `rpy: [rx, ry, rz]` — Euler angles, ZYX intrinsic, degrees. The
    canonical format: matches every common vendor's teach-pendant
    convention (Dobot, KUKA, Yaskawa, Epson SPEL+) and is what the
    forthcoming N-point calibration tool will emit.
  - `matrix: [[...]]` — 4×4 homogeneous transform. Escape hatch for
    test fixtures and externally-generated calibrations.

xyz is always in mm — no unit switch. Quaternions are not accepted; the
N-point calibration tool and human-authored entries both produce Euler
angles natively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from autoweaver.geometry import transforms

_NAME_PATTERNS = (
    re.compile(r"^arm_[a-z0-9_]+_base$"),
    re.compile(r"^arm_[a-z0-9_]+_tool_[a-z0-9_]+$"),
    re.compile(r"^fixture_[a-z0-9_]+$"),
)

_FLANGE_PATTERN = re.compile(r"^arm_[a-z0-9_]+_flange$")

_ROTATION_FIELDS = ("rpy", "matrix")
_KNOWN_FIELDS = frozenset({"name", "parent", "xyz", "rpy", "matrix"})

_RPY_CONVENTION = "zyx_intrinsic_deg"


@dataclass(frozen=True)
class FrameEdge:
    """One calibrated transform: `parent ← name`.

    `matrix` is the 4×4 homogeneous transform with translation in mm.
    """

    name: str
    parent: str
    matrix: np.ndarray


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

    _validate_name(name)
    _validate_parent(parent, name)

    xyz_raw = entry.get("xyz")
    if xyz_raw is None and "matrix" not in entry:
        raise CalibrationSchemaError("must provide 'xyz' (or use 'matrix' for full transform)")

    matrix = _build_matrix(entry, xyz_raw)
    return FrameEdge(name=name, parent=parent, matrix=matrix)


def _validate_name(name: str) -> None:
    if any(p.match(name) for p in _NAME_PATTERNS):
        return
    if name == "world":
        raise CalibrationSchemaError(
            "'world' must not appear as a frame name; it is the implicit root"
        )
    if _FLANGE_PATTERN.match(name):
        raise CalibrationSchemaError(
            f"flange frame {name!r} must not appear as a name; "
            "flange pose is dynamic and provided by the arm SDK"
        )
    raise CalibrationSchemaError(
        f"name {name!r} does not match any allowed pattern: "
        f"arm_<id>_base, arm_<id>_tool_<x>, fixture_<x>"
    )


def _validate_parent(parent: str, name: str) -> None:
    if parent == "world":
        return
    if _FLANGE_PATTERN.match(parent):
        return
    raise CalibrationSchemaError(
        f"parent {parent!r} is invalid; must be 'world' or 'arm_<id>_flange'"
    )


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
