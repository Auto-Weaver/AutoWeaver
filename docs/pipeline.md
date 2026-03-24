# Pipeline Guide

The pipeline layer is AutoWeaver's per-run execution layer.

Its job is to execute bounded acquisition and processing work, not to own business semantics or system lifecycle.

## Main Objects

### `VisionPipeline`

`VisionPipeline` runs a sequence of `ProcessStep`s and returns a `PipelineResult`.

It is the execution container for one run of one processing chain.

### `ProcessStep`

`ProcessStep` is the base abstraction for pipeline steps.

Each step receives a `PipelineContext`, may transform it, and returns the updated context.

Typical step responsibilities:

- acquire or transform image data
- append detections
- add processing metadata

### `PipelineContext`

`PipelineContext` is the mutable run context shared across steps.

It carries:

- `original_image`
- `processed_image`
- `detections`
- `metadata`

### `PipelineResult`

`PipelineResult` is the final returned object from `VisionPipeline.run()`.

It exposes:

- accumulated detections
- total processing time
- metadata
- original image
- final processed image

## Runtime Model

The current runtime model is acquisition-oriented:

- `VisionPipeline.run()` takes no image argument
- the first step is often `CaptureStep`
- `CaptureStep` fills `PipelineContext.original_image` and `processed_image`

That means a pipeline is no longer just "image in, image out". It can represent an acquisition-plus-processing chain.

## Built-In Step Categories

The core package currently includes built-in steps for:

- capture
- sharpness checking
- tiling and tile merging
- YOLO detection
- postprocessing such as filtering, sorting, and NMS

Registry-backed config construction is available for pure-config steps. `CaptureStep` is assembled in code because it needs a live camera instance.

## What Belongs in a Pipeline

Good pipeline responsibilities:

- per-run image acquisition
- deterministic preprocessing
- model inference
- postprocessing
- result shaping tied to one execution chain

Poor pipeline responsibilities:

- workflow transitions
- device lifecycle ownership
- retry policy across multiple runs
- station state coordination
- product-level business judgment over time

If logic only makes sense in the context of a business state or a long-lived control flow, it probably belongs in a task, not in a pipeline step.

## Step Design Guidance

Good pipeline steps usually:

- have narrow scope
- transform one part of the context clearly
- keep internal state minimal
- produce outputs through `processed_image`, `detections`, or `metadata`

Project-specific steps are expected. AutoWeaver core should only absorb a step if its semantics are truly reusable across projects and not tied to one product line.

## Config vs Code

Use config-driven construction when all steps can be created from pure configuration.

Use code assembly when:

- a step needs runtime objects such as cameras
- pipeline composition depends on application logic
- the chain contains project-specific steps not meant for the global registry
