# AutoWeaver

A framework for industrial vision inspection systems.

## Installation

```bash
pip install -e .
```

### Install with `uv`

If you want to consume AutoWeaver from another project and pin it to a specific Git commit,
prefer using `uv add` instead of manually editing `pyproject.toml`.

**First time adding AutoWeaver from Git:**

```bash
uv add "git+https://github.com/xinyuan/AutoWeaver.git" --rev <commit>
```

This updates both `pyproject.toml` and `uv.lock` together.

If your project already has an `autoweaver` Git source configured in `tool.uv.sources`, you can
update it to a new commit with:

```bash
uv add autoweaver --rev <commit>
```

This is the recommended workflow when bumping AutoWeaver revisions, since it avoids mismatches
between dependency declarations and the lockfile.

### Optional dependencies

**YOLO detection support:**

```bash
pip install -e ".[yolo]"
```

**Daheng industrial camera support:**

```bash
pip install -e ".[daheng]"
```

**Install everything:**

```bash
pip install -e ".[yolo,daheng]"
```

## GPU Support (Important)

`pip install autoweaver[yolo]` installs `ultralytics`, which pulls in **CPU-only PyTorch** by default.

If you need GPU acceleration (recommended for production), install the CUDA version of PyTorch **before** installing AutoWeaver:

```bash
# Example for CUDA 12.1 — adjust for your CUDA version
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

# Then install AutoWeaver
pip install -e ".[yolo]"
```

Check your CUDA version with `nvidia-smi` and pick the matching index URL from https://pytorch.org/get-started/locally/.

## Quick Start

```python
from autoweaver.pipeline import VisionPipeline, ProcessStep, Detection
from autoweaver.camera import CameraBase, MockCamera

# Create pipeline from config
pipeline = VisionPipeline.from_config({
    "pipeline": {
        "steps": [
            {"name": "tile", "type": "tile", "params": {"tile_size": 640, "overlap": 0.2}},
            {"name": "detect", "type": "yolo", "params": {"model": "best.pt", "conf": 0.25}},
            {"name": "merge", "type": "merge_tiles", "params": {"iou_threshold": 0.5}},
            {"name": "filter", "type": "filter", "params": {"min_confidence": 0.3}},
            {"name": "sort", "type": "sort", "params": {"by": "confidence"}},
        ]
    }
})

# Run detection
result = pipeline.run(image)
print(f"Found {result.detection_count} objects in {result.processing_time_ms:.1f}ms")
```

## Extending with Custom Steps

```python
from autoweaver.pipeline import ProcessStep, PipelineContext
from autoweaver.pipeline.steps import register_step

class MyCustomStep(ProcessStep):
    def process(self, ctx: PipelineContext) -> PipelineContext:
        # Your processing logic here
        ctx.metadata["my_result"] = "some_value"
        return ctx

register_step("my_step", MyCustomStep)
```

## Project Structure

```
autoweaver/
├── pipeline/    # Stateless per-frame processing pipeline
├── camera/      # Camera abstraction and implementations
├── tasks/       # Stateful task logic (v0.2)
└── workflow/    # Workflow orchestration (v0.3)
```
