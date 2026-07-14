# Pipeline Guide

> 0.5.0 下 Pipeline 本身没有变化——它仍然是单次、无状态的数据流链。变化在**使用方**：过去 Pipeline 被某个 TaskBase 子类持有、被 FrameLoopSideTask 推；现在它被 Subsystem 持有、在 `on_tick` 里调用一次。参考 `src/autoweaver/subsystem/` + pluck-hair 的 `PerceptionSubsystem`。

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

`PipelineContext` is **payload-agnostic**. It is generic over the payload
type — `PipelineContext[D]` — and makes no assumption about what a
"detection" is: `detections` is simply `list[D]`. Using it without a type
argument (`PipelineContext()`) keeps the untyped behaviour. `ProcessStep` is
generic the same way; `class MyStep(ProcessStep)` with no type argument is
still valid.

There is deliberately **no framework-level floor** — the container never
requires a payload to expose any particular field. A step that needs to look
inside a payload declares the shape it requires *next to itself*, as a
`typing.Protocol`. The built-in postprocess steps do exactly this with
`BoxLike` (see below).

### Payloads: `Detection`, `RegionDetection`, `BoxLike`

`Detection` and `RegionDetection` are **convenience payload implementations**,
not mandatory base classes. Projects may use them, subclass them, or ignore
them and carry their own payload type.

- `Detection` — a box, an `object_type` label, and a `confidence`. Exactly
  satisfies `BoxLike`.
- `RegionDetection(Detection)` — adds a **bbox-local** mask (`(h, w)` uint8
  0/255, sized to the bounding box, *not* full-frame — a full-frame mask is
  ~12 MB per detection on a 4000×3000 image) plus `area_px`. Reconstruct a
  full-frame mask by pasting the local mask at `(bbox.x1, bbox.y1)`.
- `BoxLike` — a `@runtime_checkable` `Protocol` defined in
  `steps/postprocess.py` declaring the `bbox` / `object_type` / `confidence`
  attributes the NMS, Filter, and Sort steps consume. Any object exposing
  those three qualifies structurally; nothing is forced to.

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
- YOLO instance segmentation (`YOLOSegStep`) — appends `SegmentDetection`
  (a `RegionDetection` with a bbox-local mask) to `ctx.detections`;
  `ctx.metadata["segments"]` is kept as a transitional alias
- mask application (`MaskApplyStep`)
- postprocessing such as filtering, sorting, and NMS (consume `BoxLike`)

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
