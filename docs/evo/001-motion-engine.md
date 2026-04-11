# EVO-001: Motion Engine

Date: 2026-04-11

## Background

AutoWeaver today is a perception-and-decision system. Its core loop is reactive:

```
external trigger → capture → pipeline → result → event
```

Pipeline processes data. Task interprets results. EventBus routes signals. StateMachine tracks state. Everything is event-driven and stateless within a single run. Time does not matter — a pipeline runs to completion, however long it takes.

This architecture works for inspection. It does not work for motion control.

## Why Motion Is Different

Motion control interacts with the physical world. That changes everything:

1. **Time is continuous.** A Move command takes seconds. During that time, the physical state is changing. You must continuously monitor it, not just wait for a final result.
2. **Actions have side effects.** A pipeline can be re-run safely. A motion command cannot — once the arm moves, the physical state has changed irreversibly.
3. **Feedback loops are mandatory.** A pipeline does not need to check intermediate progress. A motion sequence must: Is the arm still moving? Did the vacuum seal? Is the path still safe?
4. **Interruption must be immediate.** An e-stop, a sensor alarm, or a collision risk must halt motion within one control cycle. Pipelines have no such requirement.

These are not incremental differences. They require a different execution model.

## Dual-Engine Architecture

AutoWeaver evolves into a dual-engine framework. The two engines are independent, with different driving models:

```
AutoWeaver
├── Perception Engine (existing)
│   ├── EventBus         — event-driven, reactive
│   ├── StateMachine     — state transitions
│   ├── Pipeline         — data flow (Step → Step → Step)
│   └── Task             — assembles and consumes Pipelines
│
├── Motion Engine (new)
│   ├── BehaviorTree     — tick-driven, heartbeat
│   ├── BT Nodes         — tree node types
│   └── Action           — assembles and consumes BehaviorTrees
│
├── Sensor (new, shared)
│   └── Stateful perception entities, used by both engines
│
└── Core (existing)
    ├── EventBus
    └── StateMachine
```

Perception Engine is event-driven: something happens, the system reacts.

Motion Engine is tick-driven: a heartbeat loop continuously evaluates what to do next.

The two engines coexist. EventBus connects them at the boundary — an event can trigger a Motion Action, and a completed Action can emit an event. But internally they run on different models.

Pure inspection projects use only the Perception Engine. Projects with motion control use both. Neither engine depends on the other.

## Behavior Tree

The Motion Engine uses a Behavior Tree (BT) for execution orchestration.

### Why BT

Pipeline is a linear data-flow chain. It cannot express: "do A, wait for confirmation, then decide whether to do B or C, while continuously monitoring safety, and abort everything if timeout is exceeded."

A state machine can express this, but suffers from state/transition explosion as complexity grows. Adding a new behavior means rewiring transitions.

A Behavior Tree solves this with a single mechanism: a tree of nodes, each returning one of three states, evaluated by a periodic heartbeat.

### Tick and Three-State Return

The BT engine runs a heartbeat loop:

```
while running:
    root.tick()
    sleep(tick_period)    # typically 10-50ms
```

Each tick propagates from the root down the tree. Every node returns one of:

```
SUCCESS  — completed successfully
FAILURE  — completed with failure
RUNNING  — still in progress, tick me again next cycle
```

The tick does not visit every node. It follows the path dictated by each node's rules, stops at the first RUNNING or terminal state. The traversal path may differ on every tick.

### Node Types

BT nodes fall into four categories.

**Control nodes** govern child traversal:

| Node | Rule | Intuition |
|------|------|-----------|
| Sequence | Tick children left to right. SUCCESS → next child. FAILURE → stop, return FAILURE. RUNNING → stop, return RUNNING. Resume from last RUNNING child on next tick. | "Do these things in order" |
| Fallback | Tick children left to right. FAILURE → next child. SUCCESS → stop, return SUCCESS. RUNNING → stop, return RUNNING. | "Try this, if it fails try that" |
| Parallel | Tick all children every cycle. Return SUCCESS/FAILURE based on configurable threshold. | "Do these things simultaneously" |
| ReactiveSequence | Same as Sequence, but does NOT remember position — re-evaluates from the first child every tick. | "Do this while continuously checking that condition" |

**Decorator nodes** wrap a single child and modify its behavior:

| Node | Behavior |
|------|----------|
| Retry(n) | Re-tick child up to n times on FAILURE |
| Repeat(n) | Re-tick child up to n times on SUCCESS |
| Timeout(duration) | If child stays RUNNING beyond duration, halt it and return FAILURE |
| Inverter | Swap SUCCESS and FAILURE |
| ForceSuccess | Always return SUCCESS regardless of child result |

**Leaf nodes** are the only nodes that interact with the outside world:

| Node | Role | Side effects |
|------|------|-------------|
| Action | Execute a physical operation (Move, Actuate, Capture) | Yes — changes physical state |
| Condition | Check a predicate (sensor value, blackboard entry) | No — pure read, no side effects |

This separation is a design principle: side effects are confined to Action leaves. Everything else in the tree is pure logic.

**Wait node** is a special leaf that returns RUNNING until a duration elapses, then returns SUCCESS. It handles delays between actions.

### halt() Propagation

When a node must be interrupted (parent decides to stop it, or Timeout fires), `halt()` is called. It propagates recursively down to all RUNNING descendants, ensuring no action continues unsupervised.

### Blackboard

Nodes share data through a Blackboard — a key-value store attached to the tree. Convention: each key has exactly one writer, any number of readers. No concurrency locks needed because tick traversal is single-threaded.

Action nodes write results and feedback to the Blackboard. Condition nodes read from it. This is how perception results flow into motion decisions:

```
Capture(camera) → writes image to blackboard["image"]
Pipeline.run(blackboard["image"]) → writes result to blackboard["detect_result"]
Condition(blackboard["detect_result"].is_ok) → SUCCESS or FAILURE
```

## Action

Action is the consumer and assembler of Behavior Trees. It is the Motion Engine's counterpart to Task in the Perception Engine.

```
Perception:  Task assembles and consumes Pipelines
Motion:      Action assembles and consumes BehaviorTrees
```

### Goal / Feedback / Result

Every Action leaf node follows the Goal → Feedback → Result lifecycle:

- **Goal** — the intent (target pose, actuator state, sensor trigger)
- **Feedback** — intermediate progress (current position, force reading, completion percentage)
- **Result** — final outcome (success/failure, final state, error info)

This lifecycle spans multiple ticks:

```
tick 1:  send Goal          → RUNNING
tick 2:  read Feedback      → RUNNING
tick 3:  read Feedback      → RUNNING
tick N:  read Result        → SUCCESS / FAILURE
```

Feedback is written to the Blackboard, making it available to Condition nodes and other Action nodes within the same tree.

### Relationship to BT

Action (the high-level concept) holds and drives a BT. Action leaf nodes (inside the BT) execute individual operations. The naming overlap is intentional — they are the same concept at different scales:

- **Action (outer)** — "pick up a part, inspect it, and place it" — owns a whole tree
- **Action leaf (inner)** — "move to this position" — one node in that tree

### Timeout as Goal Parameter

Each Action leaf carries a timeout as part of its Goal, not as a separate Time concept. This is the safety baseline — every command to the physical world must have a time bound.

## Sensor

Sensors are stateful entities that exist independently of both engines.

A Sensor continuously maintains its own state. It does not know or care who reads it. Both engines access Sensors:

- Perception Engine: Pipeline reads camera frames
- Motion Engine: Condition nodes read pressure, force, distance; Action nodes trigger captures

Sensors are not BT nodes. They are not Pipeline steps. They are a shared infrastructure layer.

```
Sensor objects (independent lifecycle)
├── camera         — triggered by Action, frames read by Pipeline
├── vacuum         — continuous state, read by Condition
├── force_sensor   — continuous state, read by Condition
├── distance       — continuous state, read by Condition
└── ...
```

Some sensors are continuous (pressure, distance — always updating). Some are triggered (camera — produces data on demand). The Sensor interface accommodates both.

## Time

Time is not a first-class citizen in the Motion Engine. It is absorbed into existing mechanisms:

- **Timeout** — part of Action Goal parameters. Every action has a time bound.
- **Delay** — a Wait leaf node in the BT. Returns RUNNING until duration elapses.
- **Budget** — a Timeout Decorator wrapping a subtree. Constrains total execution time of a group of actions.

These are all expressible with BT primitives. No separate Time abstraction is needed.

The BT's tick heartbeat is itself the carrier of time — every tick is an opportunity to check elapsed time, evaluate deadlines, and react to timeouts.

## Module Layout

```
autoweaver/
├── core/
│   ├── event_bus.py
│   └── state_machine.py
├── perception/
│   ├── pipeline.py
│   ├── process_step.py
│   └── task.py
├── motion/
│   ├── behavior_tree.py       # BT engine: tick, three-state, node types
│   ├── action.py              # Action: assembles and drives BTs
│   └── nodes/                 # BT leaf/control/decorator implementations
│       ├── controls.py        # Sequence, Fallback, Parallel, ReactiveSequence
│       ├── decorators.py      # Retry, Repeat, Timeout, Inverter
│       └── leaves.py          # Action leaves, Condition, Wait
├── sensor/
│   ├── base.py                # Sensor base class
│   ├── camera.py
│   └── ...
└── comm/
    ├── comm_signal_base.py
    └── comm_side_task.py
```

## Symmetry

The two engines are deliberately symmetric:

| | Perception Engine | Motion Engine |
|---|---|---|
| Driving model | Event-driven (reactive) | Tick-driven (heartbeat) |
| Composition unit | Pipeline (data flow chain) | BehaviorTree (decision tree) |
| Atomic unit | ProcessStep | BT Node |
| Consumer | Task | Action |
| Data carrier | PipelineContext | Blackboard |
| Operates on | Images, inference results | Physical actuators, sensors |
| Time sensitivity | None | First-order concern |
| Side effects | None (pure data transform) | Yes (physical state changes) |

## Coexistence with EventBus

The Motion Engine does not replace EventBus. They serve different purposes:

- **EventBus** — inter-component communication (macro level). PlcSideTask publishes a trigger, InspectTask reacts, DbUploader logs results.
- **BehaviorTree** — intra-task execution orchestration (micro level). Within one Action, the BT sequences moves, checks sensors, branches on results.

An event can start an Action. A completed Action can emit an event. But inside the Action, the BT runs on its own tick loop, independent of EventBus.

## Design Decisions and Rationale

| Decision | Rationale |
|---|---|
| BT over state machine for motion | State machines suffer transition explosion. BT scales by adding branches. |
| BT over coroutines | BT is declarative and introspectable. Coroutines hide control flow in code. |
| Tick-driven over event-driven | Physical world requires continuous monitoring, not just reaction to discrete events. |
| Action as BT consumer, not BT node | Mirrors Task/Pipeline pattern. Keeps orchestration and consumption separate. |
| Sensor as independent module | Used by both engines. Not owned by either. |
| Time absorbed into BT | Timeout, Delay, Budget are all expressible with existing BT primitives. No separate abstraction needed. |
| Single package, directory isolation | Shared framework identity. No cross-package dependency overhead. Independent evolution via separate directories. |
| Python for BT engine | BT tick is microsecond-level computation. Real-time control lives in Rust Motion Runtime behind gRPC. Python is the right level for orchestration. |

## What This Does Not Cover

This document defines the Motion Engine's conceptual architecture. The following topics are related but out of scope:

- Coordinate transforms (camera frame → robot frame)
- Rust Motion Runtime design (EtherCAT, servo loops)
- gRPC protocol between Python and Rust
- Robot arm adapter (API/Socket communication)
- Safety Monitor design
- Specific BT trees for inductor or other projects

These will be addressed in subsequent evolution documents.
