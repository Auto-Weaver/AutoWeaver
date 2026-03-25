# Tasks and Workflow

Pipelines are not enough to express a real industrial system.

A production system also needs business intent, state progression, lifecycle control, and long-lived coordination. In AutoWeaver, that belongs to the task and workflow layers.

## `Task`

A `Task` is the business execution unit associated with a workflow state.

AutoWeaver defines `Task` as a protocol. A task implementation is expected to support:

- `attach(event_bus)`
- `tick(data)`
- `reset()`
- `close()`

`TaskBase` exists as a reusable helper base class, but the protocol matters more than inheritance.

Typical task responsibilities:

- choose or assemble pipelines
- interpret application data or recipes
- decide when work should run
- translate raw pipeline outputs into business events
- apply retry or adjustment policy

## `SideTask`

A `SideTask` is a long-lived auxiliary component that runs alongside the active task.

Typical uses:

- communication polling
- transport integration
- auxiliary services that should stay mounted across states

Unlike a pipeline, a side task is not a bounded execution chain. Unlike a task, it is not the main business actor for a workflow state.

## `WorkflowEngine`

`WorkflowEngine` is the lifecycle manager.

It is responsible for:

- owning or mounting the `EventBus`
- attaching the `StateMachine`
- switching active tasks when the state changes
- mounting side tasks
- handling graceful stop conditions

It is not responsible for being a generic frame-processing loop.

That distinction matters. The workflow layer manages lifecycle and activation, while applications remain free to decide how input data reaches `Task.tick(data)`.

## `StateMachine`

`StateMachine` turns trigger events into transitions and publishes `STATE:CHANGED`.

That makes workflow progression explicit and inspectable, rather than hiding it in scattered callback logic.

## YAML Workflow Definition

The YAML loader is a declarative entry point for workflow structure.

It describes:

- initial state
- transitions
- task type mapping
- side-task type mapping

The application still owns the final binding from those type names to concrete instances.

## Responsibility Split

Put this in a task:

- business interpretation
- pipeline choice
- retry and adjustment policy
- result broadcasting

Put this in the workflow layer:

- state transitions
- task mounting and cleanup
- side-task lifecycle
- terminal-state handling

Put this in the pipeline layer:

- acquisition and processing for one run
- inference
- postprocessing

The task/workflow split is what prevents business logic from leaking into the execution runtime and vice versa.
