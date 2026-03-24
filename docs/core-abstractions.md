# Core Abstractions

This is the most important page in the documentation set.

If AutoWeaver is to stay coherent while it evolves, these four abstractions need to stay legible:

- `Pipeline`
- `Task`
- `Workflow`
- `Event`

## `Pipeline`

### What it is

A `Pipeline` is the per-run execution chain of image acquisition and processing.

It exists to execute a bounded sequence of steps against one run context:

- capture
- preprocess
- infer
- postprocess
- shape per-run outputs

In code, this layer is centered on:

- `VisionPipeline`
- `ProcessStep`
- `PipelineContext`
- `PipelineResult`

### What it is not

A `Pipeline` is not:

- a workflow state machine
- a business process definition
- a retry controller
- a transport protocol handler
- a long-lived system orchestrator

### Boundary

The pipeline layer should stay close to execution mechanics.

If logic is mainly about one run of one processing chain, it probably belongs here. If logic is mainly about business meaning, station state, or coordination over time, it probably does not.

## `Task`

### What it is

A `Task` is the business execution unit mounted for a workflow state.

It is the layer that gives purpose to pipelines. A task typically:

- chooses which pipeline to use
- decides when a pipeline should run
- interprets results in domain terms
- emits business events
- handles retry or adjustment policies

In AutoWeaver, `Task` is a protocol. `TaskBase` is a helper base class, not the only valid implementation strategy.

### What it is not

A `Task` is not:

- a single image-processing step
- a generic event bus
- the whole system orchestrator
- a transport adapter

### Boundary

If logic answers questions like these, it usually belongs in a task:

- Why are we running this pipeline now?
- Which recipe or mode applies?
- Is the result acceptable, retryable, or terminal?
- What event should the rest of the system see?

## `Workflow`

### What it is

A `Workflow` is the lifecycle orchestration layer that organizes state transitions and task mounting.

In code, this is mainly:

- `WorkflowEngine`
- `StateMachine`
- `WorkflowDefinition`

The workflow layer exists to manage change over time:

- current state
- allowed transitions
- task switching
- side-task mounting
- start and stop lifecycle

### What it is not

A `Workflow` is not:

- the place to implement image processing
- the place to store product semantics
- a substitute for application code
- a single task's business logic

### Boundary

Workflow is about system progression, not per-run computation.

If the question is "what state is the system in, and what should become active next?", it belongs here.

## `Event`

### What it is

An `Event` is the decoupling medium of the system.

Events carry triggers and facts between components:

- external signals
- state transition triggers
- inspection completion notifications
- system lifecycle messages

The reactive layer uses events to connect otherwise independent components without tight call-chain coupling.

### What it is not

An event is not:

- a hidden function call
- a dumping ground for arbitrary unstructured state
- a replacement for clear domain modeling

### Role in the system

Events are important because they let AutoWeaver remain reactive instead of collapsing into one monolithic control loop.

Typical uses:

- side tasks publish external stimuli
- the state machine reacts to trigger events
- tasks broadcast domain outcomes
- the workflow layer responds to state-change events

### Payload discipline

AutoWeaver core does not force one universal payload schema for every event.

That flexibility is useful, but it has a cost: projects need naming and payload discipline. Event names should stay semantically meaningful, and payloads should not become opaque ad hoc blobs.

## Relationship Between the Four

The four abstractions fit together like this:

1. `Event` moves signals and facts through the system.
2. `Workflow` reacts to events and changes system state.
3. `Task` is mounted within that workflow state and expresses business intent.
4. `Pipeline` executes the concrete acquisition and processing chain used by the task.

That ordering is the spine of AutoWeaver.
