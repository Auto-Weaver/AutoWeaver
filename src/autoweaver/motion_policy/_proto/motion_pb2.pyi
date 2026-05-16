from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class WriteFieldRequest(_message.Message):
    __slots__ = ("device", "field", "value")
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    FIELD_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    device: str
    field: str
    value: Value
    def __init__(self, device: _Optional[str] = ..., field: _Optional[str] = ..., value: _Optional[_Union[Value, _Mapping]] = ...) -> None: ...

class WriteFieldResponse(_message.Message):
    __slots__ = ("ok", "error")
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    error: str
    def __init__(self, ok: bool = ..., error: _Optional[str] = ...) -> None: ...

class FieldValue(_message.Message):
    __slots__ = ("field", "value")
    FIELD_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    field: str
    value: Value
    def __init__(self, field: _Optional[str] = ..., value: _Optional[_Union[Value, _Mapping]] = ...) -> None: ...

class WriteFieldsRequest(_message.Message):
    __slots__ = ("device", "fields")
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    FIELDS_FIELD_NUMBER: _ClassVar[int]
    device: str
    fields: _containers.RepeatedCompositeFieldContainer[FieldValue]
    def __init__(self, device: _Optional[str] = ..., fields: _Optional[_Iterable[_Union[FieldValue, _Mapping]]] = ...) -> None: ...

class WriteFieldsResponse(_message.Message):
    __slots__ = ("ok", "error", "failed_field")
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    FAILED_FIELD_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    error: str
    failed_field: str
    def __init__(self, ok: bool = ..., error: _Optional[str] = ..., failed_field: _Optional[str] = ...) -> None: ...

class ReadFieldRequest(_message.Message):
    __slots__ = ("device", "field")
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    FIELD_FIELD_NUMBER: _ClassVar[int]
    device: str
    field: str
    def __init__(self, device: _Optional[str] = ..., field: _Optional[str] = ...) -> None: ...

class ReadFieldResponse(_message.Message):
    __slots__ = ("ok", "error", "value")
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    error: str
    value: Value
    def __init__(self, ok: bool = ..., error: _Optional[str] = ..., value: _Optional[_Union[Value, _Mapping]] = ...) -> None: ...

class Value(_message.Message):
    __slots__ = ("v_bool", "v_i32", "v_u32", "v_i64", "v_u64", "v_f32", "v_f64", "v_bytes")
    V_BOOL_FIELD_NUMBER: _ClassVar[int]
    V_I32_FIELD_NUMBER: _ClassVar[int]
    V_U32_FIELD_NUMBER: _ClassVar[int]
    V_I64_FIELD_NUMBER: _ClassVar[int]
    V_U64_FIELD_NUMBER: _ClassVar[int]
    V_F32_FIELD_NUMBER: _ClassVar[int]
    V_F64_FIELD_NUMBER: _ClassVar[int]
    V_BYTES_FIELD_NUMBER: _ClassVar[int]
    v_bool: bool
    v_i32: int
    v_u32: int
    v_i64: int
    v_u64: int
    v_f32: float
    v_f64: float
    v_bytes: bytes
    def __init__(self, v_bool: bool = ..., v_i32: _Optional[int] = ..., v_u32: _Optional[int] = ..., v_i64: _Optional[int] = ..., v_u64: _Optional[int] = ..., v_f32: _Optional[float] = ..., v_f64: _Optional[float] = ..., v_bytes: _Optional[bytes] = ...) -> None: ...
