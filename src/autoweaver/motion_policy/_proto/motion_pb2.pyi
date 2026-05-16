from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Motion4(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MOTION4_UNSPECIFIED: _ClassVar[Motion4]
    MOTION4_GO: _ClassVar[Motion4]
    MOTION4_JUMP: _ClassVar[Motion4]
    MOTION4_LINEAR: _ClassVar[Motion4]
    MOTION4_HOME: _ClassVar[Motion4]

class Motion6(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MOTION6_UNSPECIFIED: _ClassVar[Motion6]
    MOTION6_GO: _ClassVar[Motion6]
    MOTION6_LINEAR: _ClassVar[Motion6]
    MOTION6_HOME: _ClassVar[Motion6]
MOTION4_UNSPECIFIED: Motion4
MOTION4_GO: Motion4
MOTION4_JUMP: Motion4
MOTION4_LINEAR: Motion4
MOTION4_HOME: Motion4
MOTION6_UNSPECIFIED: Motion6
MOTION6_GO: Motion6
MOTION6_LINEAR: Motion6
MOTION6_HOME: Motion6

class ScaraGoal(_message.Message):
    __slots__ = ("device", "motion", "x", "y", "z", "u", "speed", "accel")
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    MOTION_FIELD_NUMBER: _ClassVar[int]
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    Z_FIELD_NUMBER: _ClassVar[int]
    U_FIELD_NUMBER: _ClassVar[int]
    SPEED_FIELD_NUMBER: _ClassVar[int]
    ACCEL_FIELD_NUMBER: _ClassVar[int]
    device: str
    motion: Motion4
    x: float
    y: float
    z: float
    u: float
    speed: int
    accel: int
    def __init__(self, device: _Optional[str] = ..., motion: _Optional[_Union[Motion4, str]] = ..., x: _Optional[float] = ..., y: _Optional[float] = ..., z: _Optional[float] = ..., u: _Optional[float] = ..., speed: _Optional[int] = ..., accel: _Optional[int] = ...) -> None: ...

class Arm6Goal(_message.Message):
    __slots__ = ("device", "motion", "x", "y", "z", "rx", "ry", "rz", "speed", "accel")
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    MOTION_FIELD_NUMBER: _ClassVar[int]
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    Z_FIELD_NUMBER: _ClassVar[int]
    RX_FIELD_NUMBER: _ClassVar[int]
    RY_FIELD_NUMBER: _ClassVar[int]
    RZ_FIELD_NUMBER: _ClassVar[int]
    SPEED_FIELD_NUMBER: _ClassVar[int]
    ACCEL_FIELD_NUMBER: _ClassVar[int]
    device: str
    motion: Motion6
    x: float
    y: float
    z: float
    rx: float
    ry: float
    rz: float
    speed: int
    accel: int
    def __init__(self, device: _Optional[str] = ..., motion: _Optional[_Union[Motion6, str]] = ..., x: _Optional[float] = ..., y: _Optional[float] = ..., z: _Optional[float] = ..., rx: _Optional[float] = ..., ry: _Optional[float] = ..., rz: _Optional[float] = ..., speed: _Optional[int] = ..., accel: _Optional[int] = ...) -> None: ...

class GoalResponse(_message.Message):
    __slots__ = ("ok", "error")
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    error: str
    def __init__(self, ok: bool = ..., error: _Optional[str] = ...) -> None: ...

class StatusRequest(_message.Message):
    __slots__ = ("device",)
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    device: str
    def __init__(self, device: _Optional[str] = ...) -> None: ...

class ScaraStatusResponse(_message.Message):
    __slots__ = ("ok", "error", "done", "busy", "error_code", "current_x", "current_y", "current_z", "current_u", "joint_1", "joint_2", "joint_3", "joint_4")
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    BUSY_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    CURRENT_X_FIELD_NUMBER: _ClassVar[int]
    CURRENT_Y_FIELD_NUMBER: _ClassVar[int]
    CURRENT_Z_FIELD_NUMBER: _ClassVar[int]
    CURRENT_U_FIELD_NUMBER: _ClassVar[int]
    JOINT_1_FIELD_NUMBER: _ClassVar[int]
    JOINT_2_FIELD_NUMBER: _ClassVar[int]
    JOINT_3_FIELD_NUMBER: _ClassVar[int]
    JOINT_4_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    error: str
    done: bool
    busy: bool
    error_code: int
    current_x: float
    current_y: float
    current_z: float
    current_u: float
    joint_1: float
    joint_2: float
    joint_3: float
    joint_4: float
    def __init__(self, ok: bool = ..., error: _Optional[str] = ..., done: bool = ..., busy: bool = ..., error_code: _Optional[int] = ..., current_x: _Optional[float] = ..., current_y: _Optional[float] = ..., current_z: _Optional[float] = ..., current_u: _Optional[float] = ..., joint_1: _Optional[float] = ..., joint_2: _Optional[float] = ..., joint_3: _Optional[float] = ..., joint_4: _Optional[float] = ...) -> None: ...

class Arm6StatusResponse(_message.Message):
    __slots__ = ("ok", "error", "done", "busy", "error_code", "current_x", "current_y", "current_z", "current_rx", "current_ry", "current_rz", "joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6")
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    BUSY_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    CURRENT_X_FIELD_NUMBER: _ClassVar[int]
    CURRENT_Y_FIELD_NUMBER: _ClassVar[int]
    CURRENT_Z_FIELD_NUMBER: _ClassVar[int]
    CURRENT_RX_FIELD_NUMBER: _ClassVar[int]
    CURRENT_RY_FIELD_NUMBER: _ClassVar[int]
    CURRENT_RZ_FIELD_NUMBER: _ClassVar[int]
    JOINT_1_FIELD_NUMBER: _ClassVar[int]
    JOINT_2_FIELD_NUMBER: _ClassVar[int]
    JOINT_3_FIELD_NUMBER: _ClassVar[int]
    JOINT_4_FIELD_NUMBER: _ClassVar[int]
    JOINT_5_FIELD_NUMBER: _ClassVar[int]
    JOINT_6_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    error: str
    done: bool
    busy: bool
    error_code: int
    current_x: float
    current_y: float
    current_z: float
    current_rx: float
    current_ry: float
    current_rz: float
    joint_1: float
    joint_2: float
    joint_3: float
    joint_4: float
    joint_5: float
    joint_6: float
    def __init__(self, ok: bool = ..., error: _Optional[str] = ..., done: bool = ..., busy: bool = ..., error_code: _Optional[int] = ..., current_x: _Optional[float] = ..., current_y: _Optional[float] = ..., current_z: _Optional[float] = ..., current_rx: _Optional[float] = ..., current_ry: _Optional[float] = ..., current_rz: _Optional[float] = ..., joint_1: _Optional[float] = ..., joint_2: _Optional[float] = ..., joint_3: _Optional[float] = ..., joint_4: _Optional[float] = ..., joint_5: _Optional[float] = ..., joint_6: _Optional[float] = ...) -> None: ...
