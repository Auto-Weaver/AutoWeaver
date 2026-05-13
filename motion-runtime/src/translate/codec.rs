//! Encode / decode field values to / from PDO byte buffers.

use thiserror::Error;

use crate::contract::{FieldSpec, FieldType};

use super::typed_value::TypedValue;

#[derive(Debug, Error)]
pub enum CodecError {
    #[error("buffer too small: need offset {offset}+{width} bytes, got {got}")]
    BufferTooSmall {
        offset: usize,
        width: usize,
        got: usize,
    },

    #[error("type mismatch: field expects {expected:?}, got {got:?}")]
    TypeMismatch {
        expected: FieldType,
        got: &'static str,
    },

    #[error("bool field declared bit={bit} but bit > 7")]
    BitOutOfRange { bit: u8 },

    #[error("internal: bit position on non-bool field type")]
    BitOnNonBool,
}

/// Encode a TypedValue into a byte buffer at the field's offset.
///
/// For bit-typed bools, does a read-modify-write of one byte.
/// For all other types, writes `byte_width` bytes little-endian.
pub fn encode_field(
    spec: &FieldSpec,
    value: &TypedValue,
    buf: &mut [u8],
) -> Result<(), CodecError> {
    match spec.field_type {
        FieldType::Bool => encode_bool(spec, value, buf),
        FieldType::I8 => encode_i8(spec, value, buf),
        FieldType::U8 => encode_u8(spec, value, buf),
        FieldType::I16 => encode_i16(spec, value, buf),
        FieldType::U16 => encode_u16(spec, value, buf),
        FieldType::I32 => encode_i32(spec, value, buf),
        FieldType::U32 => encode_u32(spec, value, buf),
        FieldType::I64 => encode_i64(spec, value, buf),
        FieldType::U64 => encode_u64(spec, value, buf),
        FieldType::F32 => encode_f32(spec, value, buf),
        FieldType::F64 => encode_f64(spec, value, buf),
        FieldType::Bytes => encode_bytes(spec, value, buf),
        FieldType::Str => encode_str(spec, value, buf),
    }
}

/// Decode a TypedValue from a byte buffer at the field's offset.
pub fn decode_field(spec: &FieldSpec, buf: &[u8]) -> Result<TypedValue, CodecError> {
    match spec.field_type {
        FieldType::Bool => decode_bool(spec, buf),
        FieldType::I8 => decode_fixed::<1>(spec, buf).map(|b| TypedValue::I8(b[0] as i8)),
        FieldType::U8 => decode_fixed::<1>(spec, buf).map(|b| TypedValue::U8(b[0])),
        FieldType::I16 => {
            decode_fixed::<2>(spec, buf).map(|b| TypedValue::I16(i16::from_le_bytes(b)))
        }
        FieldType::U16 => {
            decode_fixed::<2>(spec, buf).map(|b| TypedValue::U16(u16::from_le_bytes(b)))
        }
        FieldType::I32 => {
            decode_fixed::<4>(spec, buf).map(|b| TypedValue::I32(i32::from_le_bytes(b)))
        }
        FieldType::U32 => {
            decode_fixed::<4>(spec, buf).map(|b| TypedValue::U32(u32::from_le_bytes(b)))
        }
        FieldType::I64 => {
            decode_fixed::<8>(spec, buf).map(|b| TypedValue::I64(i64::from_le_bytes(b)))
        }
        FieldType::U64 => {
            decode_fixed::<8>(spec, buf).map(|b| TypedValue::U64(u64::from_le_bytes(b)))
        }
        FieldType::F32 => {
            decode_fixed::<4>(spec, buf).map(|b| TypedValue::F32(f32::from_le_bytes(b)))
        }
        FieldType::F64 => {
            decode_fixed::<8>(spec, buf).map(|b| TypedValue::F64(f64::from_le_bytes(b)))
        }
        FieldType::Bytes => {
            let slice = decode_tail(spec, buf)?;
            Ok(TypedValue::Bytes(slice.to_vec()))
        }
        FieldType::Str => {
            let slice = decode_tail(spec, buf)?;
            // Lossy decode: PDO bytes are not guaranteed to be valid UTF-8.
            // Caller (or contract author) is responsible for using Str only
            // where the controller writes UTF-8.
            Ok(TypedValue::Str(
                String::from_utf8_lossy(slice).into_owned(),
            ))
        }
    }
}

// ---------------------------------------------------------------------------
// Encoders (per-type, dispatched by encode_field)
// ---------------------------------------------------------------------------

fn type_mismatch(expected: FieldType, got: &'static str) -> CodecError {
    CodecError::TypeMismatch { expected, got }
}

fn check_room(spec: &FieldSpec, width: usize, buf_len: usize) -> Result<(), CodecError> {
    if spec.offset + width > buf_len {
        return Err(CodecError::BufferTooSmall {
            offset: spec.offset,
            width,
            got: buf_len,
        });
    }
    Ok(())
}

fn encode_bool(
    spec: &FieldSpec,
    value: &TypedValue,
    buf: &mut [u8],
) -> Result<(), CodecError> {
    let TypedValue::Bool(v) = value else {
        return Err(type_mismatch(FieldType::Bool, type_name(value)));
    };
    check_room(spec, 1, buf.len())?;

    match spec.bit {
        Some(bit) => {
            if bit > 7 {
                return Err(CodecError::BitOutOfRange { bit });
            }
            // Read-modify-write: preserve other bits in the byte.
            let mask = 1u8 << bit;
            if *v {
                buf[spec.offset] |= mask;
            } else {
                buf[spec.offset] &= !mask;
            }
        }
        None => {
            buf[spec.offset] = if *v { 1 } else { 0 };
        }
    }
    Ok(())
}

fn encode_i8(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let v = match value {
        TypedValue::I8(v) => *v,
        TypedValue::I32(v) => *v as i8, // tolerate wire-i32 carrying i8
        _ => return Err(type_mismatch(FieldType::I8, type_name(value))),
    };
    check_room(spec, 1, buf.len())?;
    buf[spec.offset] = v as u8;
    Ok(())
}

fn encode_u8(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let v = match value {
        TypedValue::U8(v) => *v,
        TypedValue::U32(v) => *v as u8,
        _ => return Err(type_mismatch(FieldType::U8, type_name(value))),
    };
    check_room(spec, 1, buf.len())?;
    buf[spec.offset] = v;
    Ok(())
}

fn encode_i16(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let v = match value {
        TypedValue::I16(v) => *v,
        TypedValue::I32(v) => *v as i16,
        _ => return Err(type_mismatch(FieldType::I16, type_name(value))),
    };
    check_room(spec, 2, buf.len())?;
    buf[spec.offset..spec.offset + 2].copy_from_slice(&v.to_le_bytes());
    Ok(())
}

fn encode_u16(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let v = match value {
        TypedValue::U16(v) => *v,
        TypedValue::U32(v) => *v as u16,
        _ => return Err(type_mismatch(FieldType::U16, type_name(value))),
    };
    check_room(spec, 2, buf.len())?;
    buf[spec.offset..spec.offset + 2].copy_from_slice(&v.to_le_bytes());
    Ok(())
}

fn encode_i32(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let TypedValue::I32(v) = value else {
        return Err(type_mismatch(FieldType::I32, type_name(value)));
    };
    check_room(spec, 4, buf.len())?;
    buf[spec.offset..spec.offset + 4].copy_from_slice(&v.to_le_bytes());
    Ok(())
}

fn encode_u32(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let TypedValue::U32(v) = value else {
        return Err(type_mismatch(FieldType::U32, type_name(value)));
    };
    check_room(spec, 4, buf.len())?;
    buf[spec.offset..spec.offset + 4].copy_from_slice(&v.to_le_bytes());
    Ok(())
}

fn encode_i64(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let TypedValue::I64(v) = value else {
        return Err(type_mismatch(FieldType::I64, type_name(value)));
    };
    check_room(spec, 8, buf.len())?;
    buf[spec.offset..spec.offset + 8].copy_from_slice(&v.to_le_bytes());
    Ok(())
}

fn encode_u64(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let TypedValue::U64(v) = value else {
        return Err(type_mismatch(FieldType::U64, type_name(value)));
    };
    check_room(spec, 8, buf.len())?;
    buf[spec.offset..spec.offset + 8].copy_from_slice(&v.to_le_bytes());
    Ok(())
}

fn encode_f32(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let TypedValue::F32(v) = value else {
        return Err(type_mismatch(FieldType::F32, type_name(value)));
    };
    check_room(spec, 4, buf.len())?;
    buf[spec.offset..spec.offset + 4].copy_from_slice(&v.to_le_bytes());
    Ok(())
}

fn encode_f64(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let TypedValue::F64(v) = value else {
        return Err(type_mismatch(FieldType::F64, type_name(value)));
    };
    check_room(spec, 8, buf.len())?;
    buf[spec.offset..spec.offset + 8].copy_from_slice(&v.to_le_bytes());
    Ok(())
}

fn encode_bytes(
    spec: &FieldSpec,
    value: &TypedValue,
    buf: &mut [u8],
) -> Result<(), CodecError> {
    let bytes = match value {
        TypedValue::Bytes(b) => b.as_slice(),
        _ => return Err(type_mismatch(FieldType::Bytes, type_name(value))),
    };
    check_room(spec, bytes.len(), buf.len())?;
    buf[spec.offset..spec.offset + bytes.len()].copy_from_slice(bytes);
    Ok(())
}

fn encode_str(spec: &FieldSpec, value: &TypedValue, buf: &mut [u8]) -> Result<(), CodecError> {
    let s = match value {
        TypedValue::Str(s) => s.as_bytes(),
        TypedValue::Bytes(b) => b.as_slice(),
        _ => return Err(type_mismatch(FieldType::Str, type_name(value))),
    };
    check_room(spec, s.len(), buf.len())?;
    buf[spec.offset..spec.offset + s.len()].copy_from_slice(s);
    Ok(())
}

// ---------------------------------------------------------------------------
// Decoders
// ---------------------------------------------------------------------------

fn decode_bool(spec: &FieldSpec, buf: &[u8]) -> Result<TypedValue, CodecError> {
    check_room(spec, 1, buf.len())?;
    let byte = buf[spec.offset];
    let value = match spec.bit {
        Some(bit) => {
            if bit > 7 {
                return Err(CodecError::BitOutOfRange { bit });
            }
            (byte >> bit) & 1 != 0
        }
        None => byte != 0,
    };
    Ok(TypedValue::Bool(value))
}

fn decode_fixed<const N: usize>(spec: &FieldSpec, buf: &[u8]) -> Result<[u8; N], CodecError> {
    check_room(spec, N, buf.len())?;
    let mut out = [0u8; N];
    out.copy_from_slice(&buf[spec.offset..spec.offset + N]);
    Ok(out)
}

/// Slice from `spec.offset` to end of buffer. Used for variable-length types.
fn decode_tail<'a>(spec: &FieldSpec, buf: &'a [u8]) -> Result<&'a [u8], CodecError> {
    if spec.offset > buf.len() {
        return Err(CodecError::BufferTooSmall {
            offset: spec.offset,
            width: 0,
            got: buf.len(),
        });
    }
    Ok(&buf[spec.offset..])
}

fn type_name(v: &TypedValue) -> &'static str {
    match v {
        TypedValue::Bool(_) => "Bool",
        TypedValue::I8(_) => "I8",
        TypedValue::U8(_) => "U8",
        TypedValue::I16(_) => "I16",
        TypedValue::U16(_) => "U16",
        TypedValue::I32(_) => "I32",
        TypedValue::U32(_) => "U32",
        TypedValue::I64(_) => "I64",
        TypedValue::U64(_) => "U64",
        TypedValue::F32(_) => "F32",
        TypedValue::F64(_) => "F64",
        TypedValue::Bytes(_) => "Bytes",
        TypedValue::Str(_) => "Str",
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::{FieldDir, FieldType};

    fn spec(offset: usize, ft: FieldType, dir: FieldDir, bit: Option<u8>) -> FieldSpec {
        FieldSpec {
            offset,
            field_type: ft,
            dir,
            bit,
        }
    }

    #[test]
    fn roundtrip_f32_at_offset_0() {
        let s = spec(0, FieldType::F32, FieldDir::Out, None);
        let mut buf = vec![0u8; 8];
        encode_field(&s, &TypedValue::F32(120.5), &mut buf).unwrap();
        // little-endian f32 of 120.5
        assert_eq!(&buf[0..4], &120.5f32.to_le_bytes());
        let decoded = decode_field(&s, &buf).unwrap();
        assert_eq!(decoded, TypedValue::F32(120.5));
    }

    #[test]
    fn roundtrip_f32_at_nonzero_offset() {
        let s = spec(4, FieldType::F32, FieldDir::Out, None);
        let mut buf = vec![0u8; 16];
        encode_field(&s, &TypedValue::F32(-7.25), &mut buf).unwrap();
        assert_eq!(&buf[4..8], &(-7.25f32).to_le_bytes());
        assert_eq!(decode_field(&s, &buf).unwrap(), TypedValue::F32(-7.25));
    }

    #[test]
    fn bit_field_rmw_preserves_other_bits() {
        // bit 0 at offset 19; other bits of buf[19] should be untouched.
        let s = spec(19, FieldType::Bool, FieldDir::Out, Some(0));
        let mut buf = vec![0u8; 32];
        buf[19] = 0b1010_1100; // other bits set

        encode_field(&s, &TypedValue::Bool(true), &mut buf).unwrap();
        assert_eq!(buf[19], 0b1010_1101); // bit 0 turned on, rest preserved

        encode_field(&s, &TypedValue::Bool(false), &mut buf).unwrap();
        assert_eq!(buf[19], 0b1010_1100); // bit 0 back off, rest still preserved
    }

    #[test]
    fn bit_field_decode() {
        let s_done = spec(0, FieldType::Bool, FieldDir::In, Some(0));
        let s_busy = spec(0, FieldType::Bool, FieldDir::In, Some(1));
        let buf = vec![0b0000_0010u8, 0, 0];
        assert_eq!(decode_field(&s_done, &buf).unwrap(), TypedValue::Bool(false));
        assert_eq!(decode_field(&s_busy, &buf).unwrap(), TypedValue::Bool(true));
    }

    #[test]
    fn whole_byte_bool() {
        let s = spec(0, FieldType::Bool, FieldDir::Out, None);
        let mut buf = vec![0u8; 4];
        encode_field(&s, &TypedValue::Bool(true), &mut buf).unwrap();
        assert_eq!(buf[0], 1);
        encode_field(&s, &TypedValue::Bool(false), &mut buf).unwrap();
        assert_eq!(buf[0], 0);
    }

    #[test]
    fn type_mismatch_reported() {
        let s = spec(0, FieldType::F32, FieldDir::Out, None);
        let mut buf = vec![0u8; 8];
        let err = encode_field(&s, &TypedValue::Bool(true), &mut buf).unwrap_err();
        assert!(matches!(err, CodecError::TypeMismatch { .. }));
    }

    #[test]
    fn buffer_too_small_reported() {
        let s = spec(4, FieldType::F64, FieldDir::Out, None);
        let mut buf = vec![0u8; 8];
        let err = encode_field(&s, &TypedValue::F64(1.0), &mut buf).unwrap_err();
        assert!(matches!(err, CodecError::BufferTooSmall { .. }));
    }

    #[test]
    fn u16_field_carries_i32_value() {
        // YAML type is u16, but Value carries it as v_u32 over the wire,
        // which we map to TypedValue::U32 in the grpc layer. The codec
        // accepts both U16 and U32 to make this work.
        let s = spec(16, FieldType::U16, FieldDir::Out, None);
        let mut buf = vec![0u8; 32];
        encode_field(&s, &TypedValue::U32(500), &mut buf).unwrap();
        assert_eq!(&buf[16..18], &500u16.to_le_bytes());

        // decode always returns the precise contract type
        assert_eq!(decode_field(&s, &buf).unwrap(), TypedValue::U16(500));
    }

    #[test]
    fn bytes_field_writes_at_offset_and_decodes_tail() {
        let s = spec(2, FieldType::Bytes, FieldDir::In, None);
        let mut buf = vec![0u8; 8];
        encode_field(
            &s,
            &TypedValue::Bytes(vec![0xDE, 0xAD, 0xBE, 0xEF]),
            &mut buf,
        )
        .unwrap();
        assert_eq!(&buf[2..6], &[0xDE, 0xAD, 0xBE, 0xEF]);

        let decoded = decode_field(&s, &buf).unwrap();
        // decode_tail returns from offset to end
        assert_eq!(
            decoded,
            TypedValue::Bytes(vec![0xDE, 0xAD, 0xBE, 0xEF, 0, 0])
        );
    }
}
