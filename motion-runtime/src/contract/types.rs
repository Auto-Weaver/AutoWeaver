//! Contract data types — deserialized from YAML, queried at runtime.

use serde::Deserialize;

/// One YAML contract file describes one device on the bus.
#[derive(Debug, Clone, Deserialize)]
pub struct Contract {
    /// Logical name used by leaf when calling `WriteField` / `ReadField`.
    pub device: String,

    /// Optional human-readable description.
    #[serde(default)]
    pub description: String,

    /// Schema version for the data-area contract between runtime and external
    /// controller code (e.g. SPEL+ project). When mismatched, leaf can refuse
    /// to operate. The check itself is leaf-side concern, runtime just exposes
    /// the value via a read_field on a well-known field.
    #[serde(default)]
    pub protocol_version: u32,

    /// How runtime decides which scanned slave this contract is for.
    pub slave_match: SlaveMatch,

    /// RxPDO / TxPDO indices and sizes. 0.7.0 only supports
    /// "contiguous byte region" PDO mapping; motors that need
    /// per-CoE-object mapping are out of scope (see 003 doc).
    pub pdo_mapping: PdoMapping,

    /// Named field table. Field offset is within the RxPDO or TxPDO
    /// (decided by `dir`), not in some global frame.
    pub fields: std::collections::BTreeMap<String, FieldSpec>,
}

/// Slave-matching predicate. At least one criterion must be set;
/// when multiple are set, ALL must match.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SlaveMatch {
    pub vendor_id: Option<u32>,
    pub product_code: Option<u32>,
    /// Case-sensitive substring match on the slave's name string from EEPROM.
    pub name_contains: Option<String>,
}

/// PDO mapping for the "contiguous byte region" form.
#[derive(Debug, Clone, Deserialize)]
pub struct PdoMapping {
    /// RxPDO (master → slave) PDO assembly index, e.g. 0x1600.
    pub rx_pdo_index: u16,
    /// Total bytes in the RxPDO image.
    pub rx_pdo_size: usize,
    /// TxPDO (slave → master) PDO assembly index, e.g. 0x1A00.
    pub tx_pdo_index: u16,
    /// Total bytes in the TxPDO image.
    pub tx_pdo_size: usize,
}

/// One field in the data area.
#[derive(Debug, Clone, Deserialize)]
pub struct FieldSpec {
    /// Byte offset within the RxPDO (dir=out) or TxPDO (dir=in).
    pub offset: usize,

    /// Field type — determines how many bytes and how to encode.
    #[serde(rename = "type")]
    pub field_type: FieldType,

    /// Direction relative to master.
    pub dir: FieldDir,

    /// Set only when field_type == Bool. Selects which bit inside
    /// the byte at `offset`. None means the whole byte is treated
    /// as bool (non-zero = true).
    #[serde(default)]
    pub bit: Option<u8>,
}

/// Field direction. Determines which PDO buffer the offset is in.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FieldDir {
    /// Master → slave. Lives in RxPDO output buffer.
    Out,
    /// Slave → master. Lives in TxPDO input buffer.
    In,
}

/// Field type. Names must match the YAML strings.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FieldType {
    Bool,
    I32,
    U32,
    I64,
    U64,
    F32,
    F64,
    /// Variable-length raw bytes. The byte count is determined by
    /// where the field sits in the PDO image — i.e. you can't have
    /// a Bytes field followed by another field at a higher offset
    /// unless you fix the size. For 0.7.0 we keep this simple:
    /// Bytes fields run from `offset` to end of the PDO image.
    Bytes,
    /// Same as Bytes but treated as a UTF-8 string by Value encoding.
    /// Kept separate from Bytes for caller clarity.
    /// Encoded/decoded via the v_bytes oneof slot.
    /// Optional: 0.7.0 may not need it; included for symmetry.
    Str,
    /// Unsigned 16-bit. Carried over gRPC as v_u32 (no v_u16 in proto).
    U16,
    /// Signed 16-bit. Carried over gRPC as v_i32.
    I16,
    /// Unsigned 8-bit. Carried over gRPC as v_u32.
    U8,
    /// Signed 8-bit. Carried over gRPC as v_i32.
    I8,
}

impl FieldType {
    /// Byte width of a single value of this type. For variable-length
    /// types (Bytes, Str) returns None.
    pub fn byte_width(self) -> Option<usize> {
        match self {
            Self::Bool => Some(1), // whole-byte default; bit fields override
            Self::I8 | Self::U8 => Some(1),
            Self::I16 | Self::U16 => Some(2),
            Self::I32 | Self::U32 | Self::F32 => Some(4),
            Self::I64 | Self::U64 | Self::F64 => Some(8),
            Self::Bytes | Self::Str => None,
        }
    }
}
