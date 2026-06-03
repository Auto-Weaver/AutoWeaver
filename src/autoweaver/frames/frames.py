"""The `Frames` runtime container — multi-arm coordinate resolution.

A `Frames` instance holds a graph of coordinate frames. Two kinds of edges:

  - **static** edges come from the calibration YAML (`world ← arm_i_base`,
    `arm_i_flange ← arm_i_tool_X`, fixtures). Their 4×4 matrices are fixed
    at load time.
  - **dynamic** edges are registered in code via :meth:`bind_dynamic`
    (flange pose, droop compensation, visual-servo residual). Their value
    is *not* stored here — it is read from a per-tick WorldBoard snapshot
    at lookup time, by the `state_key` the edge was bound to.

The single query verb is :meth:`lookup` — ``lookup(target, source)`` returns
``T(target ← source)``, the transform that maps a point expressed in
``source`` coordinates into ``target`` coordinates. The path between the two
frames is found by BFS over the graph; each edge is multiplied in, inverting
on the fly when the path traverses an edge against its stored direction.

This module knows nothing about robot SDKs, WorldBoard, or BT nodes. The
snapshot passed to :meth:`lookup` is duck-typed: anything with a
``get(key, default)`` method works (the WorldBoard Snapshot, or a plain dict
in tests). See docs/evo/008-frames.md for the design contract.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from autoweaver.frames import schema, transforms

logger = logging.getLogger(__name__)


class SnapshotLike(Protocol):
    """Anything that can supply a dynamic edge's value by key.

    The WorldBoard Snapshot satisfies this; so does a plain dict.
    """

    def get(self, key: str, default: Any = None) -> Any: ...


# <<APPEND-ERRORS>>


class FramesError(Exception):
    """Base for all frame-graph lookup failures."""


class FrameNotFound(FramesError):
    """A frame name does not exist in the graph.

    A structural / programming error — the name was never loaded from YAML
    nor registered as a dynamic edge endpoint. Fail loud.
    """


class FramesDisconnected(FramesError):
    """No path connects two frames, or a `required` dynamic edge on the
    path has no value in the current snapshot.

    The first case is structural (the graph really has two islands). The
    second is runtime: a load-bearing dynamic edge (e.g. an arm's flange
    pose) hasn't been published yet — treating it as identity would send
    the arm to the wrong place, so we refuse rather than guess.
    """


@dataclass(frozen=True)
class _Edge:
    """A directed edge `parent ← child` and how to obtain its 4×4 matrix.

    Stored in its YAML/SDK-natural orientation (parent ← child). The graph
    is traversed undirected; when a path crosses this edge the other way
    (child ← parent) the matrix is inverted on the fly.

    For a static edge, `matrix` holds the fixed transform and `state_key`
    is None. For a dynamic edge, `matrix` is None and the transform is read
    from ``snapshot[state_key]`` at lookup time.
    """

    parent: str
    child: str
    matrix: np.ndarray | None      # static edge: the fixed transform
    state_key: str | None          # dynamic edge: WorldBoard key to read
    required: bool                 # dynamic edge: missing value → raise vs identity

    @property
    def is_dynamic(self) -> bool:
        return self.state_key is not None


# <<APPEND-CLASS>>


class Frames:
    """A graph of coordinate frames with static + dynamic edges.

    Construct from a calibration YAML (static edges only). Register dynamic
    edges with :meth:`bind_dynamic` before the first lookup. Query with
    :meth:`lookup` / :meth:`transform_point`, passing the current snapshot.

    The graph is small (a cell has 5–20 frames); BFS per lookup is cheap.
    """

    IDENTITY = np.eye(4, dtype=np.float64)

    def __init__(self, calibration_path: str | Path) -> None:
        # node -> list of edges touching it (both static and dynamic share
        # this adjacency; orientation is recorded on the _Edge itself).
        self._adj: dict[str, list[_Edge]] = {}
        # remember dynamic-edge keys to reject double-binding the same edge.
        self._dynamic_edges: dict[tuple[str, str], _Edge] = {}

        for edge in schema.load(calibration_path):
            if edge.is_dynamic:
                assert edge.state_key is not None
                self.bind_dynamic(
                    edge.parent, edge.name,
                    state_key=edge.state_key, required=edge.required,
                )
            else:
                self._add_edge(
                    _Edge(
                        parent=edge.parent,
                        child=edge.name,
                        matrix=edge.matrix,
                        state_key=None,
                        required=False,
                    )
                )

        logger.info("frames: loaded graph from %s", calibration_path)
        self._log_tree(calibration_path)

    # ─── Graph construction ─────────────────────────────────────────────

    def _add_edge(self, edge: _Edge) -> None:
        self._adj.setdefault(edge.parent, []).append(edge)
        self._adj.setdefault(edge.child, []).append(edge)

    def bind_dynamic(
        self,
        parent: str,
        child: str,
        *,
        state_key: str,
        required: bool,
    ) -> None:
        """Register a dynamic edge ``parent ← child``.

        Its 4×4 transform is read from ``snapshot[state_key]`` at lookup
        time (the value a Worker publishes to WorldBoard under that key).

        Args:
            parent, child: the two frames this edge connects. The stored
                orientation is ``parent ← child`` — i.e. the snapshot value
                is expected to be ``T(parent ← child)`` (e.g. for flange
                pose, parent=arm_i_base, child=arm_i_flange, and the value
                is the SDK's ``T(base ← flange)``).
            state_key: WorldBoard key to read the value from.
            required: if True, a missing value raises FramesDisconnected
                (load-bearing edges like flange pose). If False, a missing
                value is treated as identity (optional compensation edges
                like droop / visual residual — absent until their Worker is
                wired in, and safe to skip until then).

        Raises:
            ValueError: this (parent, child) edge is already bound dynamic.
        """
        key = (parent, child)
        if key in self._dynamic_edges:
            raise ValueError(
                f"dynamic edge {parent!r} ← {child!r} already bound to "
                f"{self._dynamic_edges[key].state_key!r}"
            )
        edge = _Edge(
            parent=parent,
            child=child,
            matrix=None,
            state_key=state_key,
            required=required,
        )
        self._dynamic_edges[key] = edge
        self._add_edge(edge)

    # <<APPEND-LOOKUP>>

    # ─── Query API ──────────────────────────────────────────────────────

    def lookup(
        self, target: str, source: str, snapshot: SnapshotLike | None = None
    ) -> np.ndarray:
        """Return ``T(target ← source)`` — maps a point in `source` coords
        into `target` coords.

        Args:
            target, source: frame names. ``lookup(a, a)`` is identity.
            snapshot: per-tick value source for dynamic edges. May be omitted
                only if the path is purely static; a dynamic edge on the path
                without a snapshot raises FramesDisconnected.

        Raises:
            FrameNotFound: `target` or `source` is not in the graph.
            FramesDisconnected: no path connects them, or a required dynamic
                edge on the path has no value.
        """
        if source not in self._adj:
            raise FrameNotFound(self._explain_unknown(source))
        if target not in self._adj:
            raise FrameNotFound(self._explain_unknown(target))
        if source == target:
            return self.IDENTITY.copy()

        # BFS from source to target. We want T(target ← source): start with
        # a point in `source` and accumulate transforms that carry it toward
        # `target`. Walking source→…→target, each hop left-multiplies.
        path = self._find_path(source, target)
        if path is None:
            raise FramesDisconnected(
                f"no path from {source!r} to {target!r}; the frame graph "
                "has disconnected components"
            )

        result = self.IDENTITY.copy()
        # path is a list of frames [source, n1, n2, ..., target]. For each
        # hop (a → b) we need T(b ← a) and left-multiply: result = T(b←a) @ result.
        for a, b in zip(path, path[1:]):
            result = self._hop_matrix(a, b, snapshot) @ result
        return result

    def transform_point(
        self,
        point: np.ndarray,
        source: str,
        target: str,
        snapshot: SnapshotLike | None = None,
    ) -> np.ndarray:
        """Transform a 3-point (or homogeneous 4-vector) from `source` to
        `target` coordinates. Returns a 3-vector."""
        p = np.asarray(point, dtype=np.float64)
        if p.shape == (3,):
            p = np.array([p[0], p[1], p[2], 1.0])
        elif p.shape != (4,):
            raise ValueError(f"point must be shape (3,) or (4,), got {p.shape}")
        out = self.lookup(target, source, snapshot) @ p
        return out[:3]

    def can_lookup(
        self, target: str, source: str, snapshot: SnapshotLike | None = None
    ) -> bool:
        """True if :meth:`lookup` would succeed for these args + snapshot."""
        try:
            self.lookup(target, source, snapshot)
            return True
        except FramesError:
            return False

    # ─── Path finding + per-edge resolution ─────────────────────────────

    def _find_path(self, source: str, target: str) -> list[str] | None:
        """BFS shortest path source→target over the undirected graph.
        Returns the node sequence inclusive of both ends, or None."""
        prev: dict[str, str | None] = {source: None}
        q: deque[str] = deque([source])
        while q:
            node = q.popleft()
            if node == target:
                break
            for edge in self._adj.get(node, ()):
                nxt = edge.child if edge.parent == node else edge.parent
                if nxt not in prev:
                    prev[nxt] = node
                    q.append(nxt)
        if target not in prev:
            return None
        # Reconstruct source→target.
        path: list[str] = []
        cur: str | None = target
        while cur is not None:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    def _hop_matrix(
        self, a: str, b: str, snapshot: SnapshotLike | None
    ) -> np.ndarray:
        """Return ``T(b ← a)`` for one graph hop from `a` to `b`.

        The edge is stored as ``parent ← child``. If the hop direction
        matches the stored orientation (a=child, b=parent) we use the
        matrix as-is; otherwise we invert it on the fly — this is the only
        place inversion happens at runtime.
        """
        edge = self._edge_between(a, b)
        natural = self._edge_value(edge, snapshot)   # T(parent ← child)
        if a == edge.child and b == edge.parent:
            return natural                            # going child → parent
        return transforms.invert(natural)            # going parent → child

    def _edge_between(self, a: str, b: str) -> _Edge:
        """The edge connecting `a` and `b` (either orientation).

        If both a static and a dynamic edge connect the same pair, the
        dynamic one wins — a compensation edge is meant to override the
        nominal static placement.
        """
        chosen: _Edge | None = None
        for edge in self._adj.get(a, ()):
            if (edge.parent, edge.child) in ((a, b), (b, a)):
                if edge.is_dynamic:
                    return edge
                chosen = edge
        if chosen is None:
            # _find_path only emits adjacent hops, so this is a logic error.
            raise FramesDisconnected(f"no edge between {a!r} and {b!r}")
        return chosen

    def _edge_value(
        self, edge: _Edge, snapshot: SnapshotLike | None
    ) -> np.ndarray:
        """The edge's ``T(parent ← child)`` matrix.

        Static: the fixed matrix. Dynamic: read from the snapshot, applying
        the required/optional missing-value policy.
        """
        if not edge.is_dynamic:
            assert edge.matrix is not None  # static edges always carry one
            return edge.matrix

        value = None if snapshot is None else snapshot.get(edge.state_key, None)
        if value is None:
            if edge.required:
                raise FramesDisconnected(
                    f"required dynamic edge {edge.parent!r} ← {edge.child!r} "
                    f"has no value at {edge.state_key!r} "
                    f"(its Worker hasn't published yet); refusing to assume "
                    "identity for a load-bearing edge"
                )
            return self.IDENTITY  # optional edge absent → no-op
        return np.asarray(value, dtype=np.float64)

    # <<APPEND-INTROSPECT>>

    # ─── Introspection / debugging ──────────────────────────────────────

    def frame_names(self) -> list[str]:
        """All frame names currently in the graph (static + dynamic)."""
        return sorted(self._adj)

    def describe_path(self, target: str, source: str) -> list[dict[str, Any]]:
        """Describe the hops `lookup(target, source)` would traverse, without
        evaluating any matrix. Each entry flags whether the hop is dynamic
        and (if so) which snapshot key feeds it — for debugging which links
        in a chain are read live.

        Raises the same FrameNotFound / FramesDisconnected as lookup.
        """
        if source not in self._adj:
            raise FrameNotFound(self._explain_unknown(source))
        if target not in self._adj:
            raise FrameNotFound(self._explain_unknown(target))
        path = self._find_path(source, target) if source != target else [source]
        if path is None:
            raise FramesDisconnected(f"no path from {source!r} to {target!r}")
        hops: list[dict[str, Any]] = []
        for a, b in zip(path, path[1:]):
            edge = self._edge_between(a, b)
            hops.append(
                {
                    "from": a,
                    "to": b,
                    "dynamic": edge.is_dynamic,
                    "state_key": edge.state_key,
                    "required": edge.required if edge.is_dynamic else None,
                    "inverted": not (a == edge.child and b == edge.parent),
                }
            )
        return hops

    def _explain_unknown(self, name: str) -> str:
        return (
            f"frame {name!r} is not in the graph; known frames: "
            f"{self.frame_names()}. (A frame only exists once some edge — "
            "static in YAML or bound via bind_dynamic() — names it.)"
        )

    def _log_tree(self, path: str | Path) -> None:
        """Print the frame topology at INFO level so a human can sanity-check
        what was loaded by glancing at startup logs. Rooted at `world` if
        present; dynamic edges are annotated with their snapshot key."""
        lines = [f"frames: topology loaded from {path}"]
        roots = ["world"] if "world" in self._adj else sorted(self._adj)
        seen: set[str] = set()
        for root in roots:
            if root not in seen:
                lines.append(root)
                self._render_subtree(root, "", lines, seen)
        # Anything unreached from the chosen root(s) — flag it.
        for name in sorted(self._adj):
            if name not in seen:
                lines.append(f"⚠ {name} (not connected to {roots[0]!r})")
                seen.add(name)
        logger.info("\n".join(lines))

    def _render_subtree(
        self,
        node: str,
        prefix: str,
        lines: list[str],
        seen: set[str],
        *,
        edge_tag: str = "",
        is_root: bool = False,
    ) -> None:
        """Render `node` and its descendants. `prefix` is the indentation
        already laid down by ancestors (the connector for `node` itself is
        included by the caller)."""
        seen.add(node)
        child_edges = sorted(
            (e for e in self._adj.get(node, ())
             if e.parent == node and e.child not in seen),
            key=lambda e: e.child,
        )
        for i, edge in enumerate(child_edges):
            last = i == len(child_edges) - 1
            connector = "└── " if last else "├── "
            tag = f"  (dynamic ← {edge.state_key})" if edge.is_dynamic else ""
            lines.append(f"{prefix}{connector}{edge.child}{tag}")
            child_prefix = prefix + ("    " if last else "│   ")
            self._render_subtree(edge.child, child_prefix, lines, seen)




