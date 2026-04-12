# AutoWeaver Docs

These docs are written as system-definition documents first.

That means they prioritize:

- what each abstraction is
- what it is not
- where the layer boundaries are
- which parts belong in core and which parts belong in application code

They intentionally do not rely on many code examples. Examples are useful, but in a framework that evolves quickly they should stay secondary to clear conceptual contracts.

## Reading Order

1. [Core Abstractions](core-abstractions.md)
2. [Architecture](architecture.md)
3. [Pipeline Guide](pipeline.md)
4. [Tasks and Workflow](tasks-and-workflow.md)
5. [Camera and Communication](camera-and-comm.md)
6. [Getting Started](getting-started.md)

## Reading Strategy

- If you want to understand what AutoWeaver is, start with [Core Abstractions](core-abstractions.md).
- If you want to understand how the layers fit together, read [Architecture](architecture.md).
- If you are building vision execution chains, read [Pipeline Guide](pipeline.md).
- If you are building a station-level system, read [Tasks and Workflow](tasks-and-workflow.md).
- If you are integrating devices, PLCs, or transports, read [Camera and Communication](camera-and-comm.md).

## Release Notes

- [0.4.3 - Perception Runtime Milestone](release-notes-0.4.3.md)

## Source of Truth

The source code is the final truth for exact signatures and implementation details.

The role of these docs is to make the architectural meaning of the system explicit, so that humans and AI tools do not have to infer it only from scattered implementation details.
