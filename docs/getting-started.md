# Getting Started

This page is intentionally short.

Its purpose is not to teach the whole framework through examples. It points you into the right reading order.

## Step 1: Read EVO-007

Before writing any code, read **[EVO-007: BT + Worker + Task 三层模型](evo/007-bt-worker-task.md)**. It defines what the framework actually is — BT 树是唯一主动调度方、Worker 被动响应、WorldBoard 做共享状态。Everything else is a detail on top.

If the BT / Worker / Task / note-vs-state split is still blurry after one pass, read it again. Placing logic in the wrong layer comes from incomplete mental model.

## Step 2: Map the Layers

Then skim [Architecture](architecture.md) for the layer map:

- **BT tree** — the only active scheduler; routes work through `notify` + `wait_for`
- **Worker** — passive unit of "one piece of the outside world" (camera owner, arm connection, comm link)
- **Task** — Worker-internal collaborator; not visible across the framework boundary
- **Pipeline** — per-run data flow (VisionPipeline), used inside a Worker
- **BTClock + WorldBoard** — framework-provided clock and shared state

## Step 3: Decide Your Entry Point

### Just run a pipeline?

If you just need "capture → run a YOLO chain → return result", start with [Pipeline Guide](pipeline.md). A pipeline by itself is a pure function.

### Build a full station?

If you need continuous tick-driven behavior — motion control, sensor fusion, multi-worker coordination — you need the Worker + BT tree path. Start by:

1. Write one `Worker` subclass for each piece of the outside world.
2. Write a BT tree that orchestrates them via `notify_and_wait` and `wait_for`.
3. Wire them all into a single `BTClock(world_board=board)`.

Reference implementation: pluck-hair's `src/subsystems/focus_subsystem.py` — note this is **still the 0.5.x style** (single Subsystem holding the whole z-scan state machine). Rewriting it as BT + Workers is the upcoming demo; see [migration-0.6.md](migration-0.6.md) for the target shape.

## Step 4: Keep Core and Application Separate

Ask these early:

- Is this reusable across projects, or only for one product family?
- Is this per-run execution logic, or stateful business logic?
- Is this transport/device plumbing, or domain semantics?

Answers decide whether something belongs in autoweaver core or in the application package built on top of it.

## Installation

Develop inside the repository:

```bash
uv sync
```

With optional extras:

```bash
uv sync --extra yolo --extra daheng --extra websocket
```

Consume autoweaver from another project:

```toml
# in your pyproject.toml
[tool.uv.sources]
autoweaver = { git = "https://github.com/Einstellung/AutoWeaver.git", rev = "<commit>" }
```

## Reading Order

1. [EVO-007: BT + Worker + Task](evo/007-bt-worker-task.md) — **start here**
2. [Architecture](architecture.md) — layer map
3. [EVO-005: Subsystem 对接细节](evo/005-bt-world-bridge.md) — note vs state, double-board model (terminology still applies; replace "Subsystem" → "Worker")
4. [Pipeline Guide](pipeline.md)
5. [Camera and Communication](camera-and-comm.md)
6. [Migration 0.5](migration-0.5.md) — if coming from an 0.4.x codebase
7. [Migration 0.6](migration-0.6.md) — if coming from 0.5.x

## First Implementation Rule

Don't start by writing a lot of generic code.

Start by placing one real piece of logic in the right layer (usually "this is a Worker that owns one piece of the outside world", or "this is a BT node that decides flow"). Once that placement is correct, the rest of the framework extends coherently.
