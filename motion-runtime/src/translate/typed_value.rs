//! TypedValue — runtime-internal value representation.
//!
//! Sits between proto `Value` (with oneof Kind) and raw bytes.
//! The translate module operates only on TypedValue so it has no
//! direct dependency on proto-generated types — that conversion
//! lives in the grpc module.

/// All field value variants. Mirrors the proto `Value` oneof, plus the
/// small integer types (i8/i16/u8/u16) that don't have direct oneof slots —
/// over the wire they are carried as i32/u32 and converted here.
#[derive(Debug, Clone, PartialEq)]
pub enum TypedValue {
    Bool(bool),
    I8(i8),
    U8(u8),
    I16(i16),
    U16(u16),
    I32(i32),
    U32(u32),
    I64(i64),
    U64(u64),
    F32(f32),
    F64(f64),
    Bytes(Vec<u8>),
    Str(String),
}
