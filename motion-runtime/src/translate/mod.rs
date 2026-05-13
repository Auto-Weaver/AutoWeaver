//! Field value ↔ byte translation.
//!
//! Pure functions: take a FieldSpec + a byte slice, encode/decode a value.
//! No knowledge of EtherCAT, gRPC, or any I/O. Tested in isolation.

mod typed_value;
mod codec;

pub use codec::{decode_field, encode_field, CodecError};
pub use typed_value::TypedValue;
