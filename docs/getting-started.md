# Getting Started

This page is intentionally short.

Its purpose is not to teach the whole framework through examples. It points you into the right reading order.

## Step 1: Read EVO-006

Before writing any code, read **[EVO-006: BT 全局时钟与 Subsystem 模型](evo/006-bt-clock-and-subsystem.md)**. It defines what the framework actually is — BTClock 驱动、Subsystem 被动响应、WorldBoard 做共享状态。Everything else is a detail on top.

If the BT Clock / Subsystem / note-vs-state split is still blurry after one pass, read it again. Placing logic in the wrong layer comes from incomplete mental model.

## Step 2: Map the Layers

Then skim [Architecture](architecture.md) for the layer map:

- **Pipeline** — per-run data flow (VisionPipeline)
- **Sensor** — passive device driver (CameraBase implements Sensor)
- **Subsystem** — the unit of business logic; one per piece of the outside world
- **BT tree** (optional) — explicit flow orchestration using NotifyLeaf / WaitFor / MotionLeaf
- **BTClock + WorldBoard** — framework-provided clock and shared state

## Step 3: Decide Your Entry Point

### Just run a pipeline?

If you just need "capture → run a YOLO chain → return result", start with [Pipeline Guide](pipeline.md). A pipeline by itself is a pure function.

### Build a full station?

If you need continuous tick-driven behavior — motion control, sensor fusion, multi-subsystem coordination — you need the Subsystem + BTClock path. Start by:

1. Write one `Subsystem` subclass for each piece of the outside world.
2. Wire them all into a single `BTClock(world_board=board)`.
3. Use `NotifyLeaf` / `WaitFor` in a BT tree if you want explicit flow.

Reference implementation: pluck-hair's `src/subsystems/focus_subsystem.py` — a 250-line example that covers the common patterns (state declaration, note handling, cross-namespace reads).

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

1. [EVO-006: BT Clock + Subsystem](evo/006-bt-clock-and-subsystem.md) — **start here**
2. [Architecture](architecture.md) — layer map
3. [EVO-005: Subsystem 对接细节](evo/005-bt-world-bridge.md) — note vs state, double-board model
4. [Pipeline Guide](pipeline.md)
5. [Camera and Communication](camera-and-comm.md)
6. [Migration 0.5](migration-0.5.md) — if coming from an 0.4.x codebase

## First Implementation Rule

Don't start by writing a lot of generic code.

Start by placing one real piece of logic in the right layer (usually "this is a Subsystem that owns one piece of the outside world"). Once that placement is correct, the rest of the framework extends coherently.
