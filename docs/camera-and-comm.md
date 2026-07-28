# Camera and Communication

> **0.5.x 命名变更（comm）**：
> - `CommSignalBase` → `CommBase`（协议契约）
> - `ModbusAdapter` → `ModbusProtocol`，`WebSocketAdapter` → `WebSocketProtocol`，`WebSocketServerAdapter` → `WSServerProtocol`（具体协议实现）
> - `CommSubsystem` 名字不变（Subsystem 模板）
> - 直接 break，没有 alias。详见 [migration-0.5.md](migration-0.5.md)。

> **0.5.0 base**：
> - `CameraBase` 继承了 `autoweaver.Sensor`——主入口是 `snapshot()` / `is_open()`；旧名 `capture()` / `is_opened()` 作为 back-compat alias 仍然可用。相机应该被 Subsystem 持有，不再被业务代码直接调用。
> - `CommSideTask` 已删除，换成 `CommSubsystem`——继承 `Subsystem`，内部 polling 走 `run_background` 守护线程，protocol 在 `on_start` 打开、`on_stop` 关闭。

Industrial systems live at the boundary between algorithms, devices, and external control systems.

AutoWeaver provides abstractions for both camera access and communication, but it deliberately stops short of embedding project-specific semantics into those abstractions.

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

### `observe()` — the reading with its identity attached (EVO-011)

`snapshot()` returns a bare array: no idea who took it, when, or under what
exposure. `observe()` returns a `CameraObservation` instead — the same pixels
plus the identity only the device can supply:

```python
camera.role = "nest"              # the device's position in the system
observation = camera.observe()
observation.id                     # monotonic per role — usable as a freshness gate
observation.source                 # "nest", not "DahengCamera"
observation.captured_at            # monotonic clock at acquisition
observation.conditions             # exposure / gain / white balance / trigger mode
observation.crop(x, y, w, h)       # a view, with lineage: .to_root(u, v) maps back
```

`observe()` raises if no `role` has been assigned. Falling back to the class name
would put two Daheng cameras back to both being `"DahengCamera"`, which is the
defect this exists to remove.

Reads are serialised per instance — **a lock, not a thread**; `Sensor` still has
no heartbeat of its own. That is what makes a hand-written `ThreadSafe*` wrapper
unnecessary.

`snapshot()` / `capture()` / `is_opened()` are unchanged and keep working.

> **Subclassing note.** `capture()` delegates *to* `snapshot()`. A subclass that
> customises acquisition must override **`snapshot()`**; overriding only
> `capture()` leaves `observe()` bypassing the customisation, with no error.

### Observers

A Sensor pushes each observation to whatever registered for it — preview, video
recording, archiving, logging, detection are all just observers. Adding a second
camera therefore stops being a change to every consumer.

```python
camera.set_slow_dispatcher(worker.run_async)   # required before any SLOW observer
camera.add_observer(preview)                   # FAST: runs inline on the tick thread
camera.add_observer(recorder)                  # SLOW: handed to the dispatcher
```

Every observer declares `speed`. There is no default, because the safe-looking
one (`FAST`) is precisely the wrong guess for a video encoder — and the symptom,
"the whole BT went choppy", is nearly impossible to pin on a single observer.
Registering a `SLOW` observer with no dispatcher raises immediately.

The drive chain is `BT → Sensor → Observer`. Acquisition *modes* (live, on
demand, burst) are BT-side orchestration; the Sensor holds no scheduler.

## Camera Ownership

Camera lifecycle should normally belong to a Subsystem.

That means:

- the Subsystem opens and closes the camera
- the pipeline uses the live camera object through `CaptureStep`
- the pipeline does not become the global owner of device lifecycle

This keeps execution logic and resource ownership separate.

## Communication Layer

Comm is layered into four conceptual layers. The first three sit in
`autoweaver.comm`; the fourth is the application's responsibility.

```
Layer 1: CommBase                ← protocol contract (receive/send/close)
Layer 2: ModbusProtocol / ...    ← concrete protocol mechanics
Layer 3: CommSubsystem           ← Subsystem template that adopts a protocol
Layer 4: application code        ← names a connection by peer (Nova5Link,
                                   PlcLink) and gives messages business
                                   meaning
```

The split between Layer 2 and Layer 4 matters: **a protocol is "what
language we use to talk"** (Modbus, WebSocket, …); **a Link is "who we
are talking to"** (Nova5, PLC, …). Two Links can share one protocol.
The same Link can switch protocols later without renaming.

### Layer 1 — `CommBase`

`CommBase` defines the protocol contract:

- `receive() -> dict | None` (non-blocking)
- `send(message: dict)`
- `close()`

It is intentionally protocol-focused and business-neutral.

### Layer 2 — Built-In Protocols

Concrete `CommBase` implementations shipped with AutoWeaver:

- `ModbusProtocol` — Modbus TCP, single-register handshake by default
- `WebSocketProtocol` — WebSocket client (JSON frames by default)
- `WSServerProtocol` — single-client WebSocket server

These classes know nothing about devices or business meaning. They
do not decide what your application means by `reach_surface`,
`pick_done`, `retry`, or `reset`.

You can use a Protocol on its own (e.g. for a one-off diagnostic
script) — just instantiate, call `receive()`/`send()`/`close()`. It is
not required to be wrapped in a `CommSubsystem`.

### Layer 3 — `CommSubsystem`

`CommSubsystem` wraps a `CommBase` protocol with the standard Subsystem
lifecycle:

- the protocol's polling loop runs as a `run_background` daemon thread
- inbound messages reach `handle_message(msg)` on the polling thread
- the protocol is closed in `on_stop`

`handle_message` is the only required override. To keep state mutation
on the tick thread, hand off to other Subsystems by passing notes
(via `WorldBoard.pass_note`) instead of writing state from the polling
thread.

### Layer 4 — Application "Links"

Layer 4 is where each connection is named for **its peer** and gets
business meaning. Application code subclasses `CommSubsystem`, gives it
a name like `Nova5Link` or `PlcLink`, and translates messages into
notes/state:

```python
class PlcLink(CommSubsystem):
    name = "plc_link"

    def __init__(self):
        super().__init__(
            ModbusProtocol(host="192.168.1.10", port=502),
        )

    def handle_message(self, msg):
        if msg.get("type") == "request_target":
            self._board.pass_note("picker", "request_target", msg)
```

If the same peer changes protocol later (e.g. PLC moves from Modbus
to EtherCAT), only the protocol argument changes — the Link's name and
its place in the architecture stay put.

## Boundary Rule

Keep these responsibilities separate:

- **Layer 2 (protocols)** is about wire mechanics
- **Layer 3 (CommSubsystem)** is about lifecycle and threading
- **Layer 4 (Links)** is about *who* the peer is and *what* its messages mean

That separation is what keeps AutoWeaver reusable across different
industrial projects rather than freezing it around one station's
semantics.
