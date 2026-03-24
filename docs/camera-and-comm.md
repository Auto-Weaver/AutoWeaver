# Camera and Communication

Industrial systems live at the boundary between algorithms, devices, and external control systems.

AutoWeaver provides abstractions for both camera access and communication transport, but it deliberately stops short of embedding project-specific semantics into those abstractions.

## Camera Layer

### `CameraBase`

`CameraBase` defines the generic camera contract:

- `open()`
- `close()`
- `capture()`
- `is_opened()`
- `get_frame_size()`
- `set_exposure_time()`
- `set_gain()`

This contract exists so the rest of the system can remain camera-agnostic.

### Built-In Implementations

- `MockCamera`
- `DahengCamera`

## Camera Ownership

Camera lifecycle should normally belong to the task or application layer.

That means:

- the application opens and closes the camera
- the pipeline uses the live camera object through `CaptureStep`
- the pipeline does not become the global owner of device lifecycle

This keeps execution logic and resource ownership separate.

## Communication Layer

### `CommSignalBase`

`CommSignalBase` defines the transport contract:

- `receive()`
- `send(message)`
- `close()`

It is intentionally protocol-focused and business-neutral.

### Built-In Adapters

- `ModbusAdapter`
- `WebSocketAdapter`

These adapters handle transport mechanics. They should not decide what your application means by `reach_surface`, `pick_done`, `retry`, or `reset`.

## `CommSideTask`

`CommSideTask` is the bridge between a transport and the reactive system.

It provides:

- a polling loop
- message draining
- a hook for message handling
- access to the event bus through task-style lifecycle

This is the right place to translate transport messages into AutoWeaver events.

## Boundary Rule

Keep these responsibilities separate:

- camera adapters are about device access
- comm adapters are about transport I/O
- side tasks are about integration into the reactive runtime
- business meaning stays in application tasks or domain code

That separation is what keeps AutoWeaver reusable across different industrial projects rather than freezing it around one station's semantics.
