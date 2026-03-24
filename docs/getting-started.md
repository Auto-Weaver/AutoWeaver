# Getting Started

This page is intentionally short.

Its purpose is not to teach the whole framework through examples. Its purpose is to help you enter the system in the right order.

## Step 1: Understand the Four Core Abstractions

Before writing code, read [Core Abstractions](core-abstractions.md).

If `Pipeline`, `Task`, `Workflow`, and `Event` are still blurry, you will almost certainly place logic in the wrong layer.

## Step 2: Decide Your Integration Scope

There are two common entry points.

### Pipeline-first

Choose this if you only need execution chains for now:

- camera acquisition
- preprocessing
- inference
- postprocessing

In this mode, start with [Pipeline Guide](pipeline.md).

### Workflow-first

Choose this if you are building a full station or cell:

- external triggers
- state transitions
- multiple tasks
- side-task based communication

In this mode, read [Architecture](architecture.md) and [Tasks and Workflow](tasks-and-workflow.md) early.

## Step 3: Keep Core and Application Logic Separate

Ask these questions early:

- Is this reusable across projects, or only for one product family?
- Is this per-run execution logic, or stateful business logic?
- Is this transport/device plumbing, or domain semantics?

Those answers decide whether something belongs in AutoWeaver core or in the application package built on top of it.

## Installation

Develop inside the repository:

```bash
uv sync
```

With optional extras:

```bash
uv sync --extra yolo --extra daheng --extra websocket
```

Consume AutoWeaver from another project:

```bash
uv add "git+https://github.com/Auto-Weaver/AutoWeaver.git" --rev <commit>
```

## Practical Reading Order

1. [Core Abstractions](core-abstractions.md)
2. [Architecture](architecture.md)
3. [Pipeline Guide](pipeline.md)
4. [Tasks and Workflow](tasks-and-workflow.md)
5. [Camera and Communication](camera-and-comm.md)

## First Implementation Rule

Do not start by writing a lot of generic code.

Start by placing one real piece of logic in the right layer. Once that placement is correct, the rest of the framework becomes much easier to extend coherently.
