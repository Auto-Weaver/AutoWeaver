# AutoWeaver

AutoWeaver is a reactive framework for industrial systems.

Today it is aimed at general industrial quality inspection. In the longer run, it is intended to become a runtime foundation for broader industrial autonomy: not just running algorithms, but organizing perception, device interaction, control flow, and business logic in one coherent architecture.

The name reflects the ambition. AutoWeaver is meant to be the weaver of algorithmic flows.

## Core Abstractions

- `Pipeline`: the per-run execution chain for acquisition and processing
- `Task`: the business execution unit that uses pipelines and interprets results
- `Workflow`: the lifecycle and state orchestration layer that mounts tasks
- `Event`: the decoupling medium that carries triggers and facts through the system

These are the common-layer concepts of AutoWeaver. If they are clear, the rest of the package is much easier to place.

## Documentation

- [Docs Index](docs/README.md)
- [Core Abstractions](docs/core-abstractions.md)
- [Architecture](docs/architecture.md)
- [Pipeline Guide](docs/pipeline.md)
- [Tasks and Workflow](docs/tasks-and-workflow.md)
- [Camera and Communication](docs/camera-and-comm.md)
- [Getting Started](docs/getting-started.md)

## Documentation Style

These docs intentionally prioritize:

- definition over tutorial
- boundaries over large code examples
- contracts over patterns copied from one project

The source code remains the final truth for exact implementation details. The docs exist to make the architecture legible to humans and AI readers.

## Installation

Develop inside the repository:

```bash
uv sync
```

Install optional extras when needed:

```bash
uv sync --extra yolo --extra daheng --extra websocket
```

`pip` also works for local editable installs:

```bash
pip install -e .
pip install -e ".[yolo,daheng,websocket]"
```

Consume AutoWeaver from another project with `uv`:

```bash
uv add "git+https://github.com/Auto-Weaver/AutoWeaver.git" --rev <commit>
```

If your project already has an `autoweaver` Git source configured, bump it with:

```bash
uv add autoweaver --rev <commit>
```

## Optional Dependencies

- `yolo`: installs `ultralytics`
- `daheng`: installs `iai-gxipy`
- `websocket`: installs `websockets`

## GPU Support

`ultralytics` will typically pull in CPU-only PyTorch by default. If you need GPU acceleration, install the correct CUDA build of PyTorch before installing the `yolo` extra.

Example for CUDA 12.1:

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
pip install -e ".[yolo]"
```

## Package Layout

```text
src/autoweaver/
├── camera/      # Camera abstraction and implementations
├── comm/        # Communication transports and side-task bridge
├── pipeline/    # Per-run execution pipeline and built-in steps
├── reactive/    # Event bus and state machine
├── tasks/       # Task protocols and reusable task utilities
└── workflow/    # Workflow engine and YAML loader
```
