# Architecture

AutoWeaver is a layered reactive runtime for industrial systems.

Its purpose is not only to run algorithms, but to separate execution, coordination, and business meaning cleanly enough that the whole system can grow without collapsing into one script.

## Layer Map

```text
Application / Domain Layer
  Product semantics, recipes, station rules, business-specific pipelines

Workflow Layer
  WorkflowEngine
  Task / SideTask lifecycle

Reactive Layer
  EventBus
  StateMachine

Execution Layer
  VisionPipeline
  ProcessStep
  CameraBase / CommSignalBase
```

## Why This Split Exists

Industrial systems mix very different kinds of logic:

- device I/O
- per-run algorithm execution
- long-lived control flow
- product semantics
- external coordination

If those concerns are mixed together, the system becomes hard to reason about and harder to reuse. AutoWeaver's architecture exists to stop that collapse.

## Layer Responsibilities

### Execution Layer

This layer runs bounded processing work.

Typical responsibilities:

- capture a frame
- transform images
- run inference
- collect detections
- return per-run outputs

This layer should remain reusable and domain-light.

### Reactive Layer

This layer routes information.

Typical responsibilities:

- publish and subscribe to events
- map trigger events to state transitions
- emit state change notifications

This layer should know as little as possible about business semantics.

### Workflow Layer

This layer manages lifecycle.

Typical responsibilities:

- attach the state machine to the event bus
- mount and unmount tasks
- mount side tasks
- stop on terminal conditions or external signals

The workflow layer is about system progression over time, not image processing.

### Application Layer

This layer provides industrial meaning.

Typical responsibilities:

- product-specific region semantics
- recipe interpretation
- retry rules
- reporting logic
- line-specific protocols and events

This is where AOI, station, or robotics domain logic should live.

## Event-Centered Flow

AutoWeaver is reactive, so the architecture is best understood through event flow:

1. Some external or internal component produces an event.
2. The reactive layer distributes that event.
3. The state machine may translate it into a workflow transition.
4. The workflow layer updates which task is active.
5. Tasks and side tasks react according to their responsibility.
6. Pipelines are invoked where concrete execution is needed.

The key point is that no single layer should try to own all of that.

## Core vs Application Code

Belongs in AutoWeaver core:

- reusable runtime abstractions
- generic pipeline mechanics
- device-neutral contracts
- transport adapters without domain semantics
- workflow and reactive primitives

Belongs in application code:

- product-specific step semantics
- recipe models
- defect taxonomy
- station events and payload meaning
- business decisions around pass, fail, retry, or escalation

If a component only makes sense for one product line or one station, it usually does not belong in core.
