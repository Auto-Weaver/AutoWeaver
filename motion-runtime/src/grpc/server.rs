//! gRPC server implementing the WriteField / ReadField surface.
//!
//! The implementation is a thin shell:
//!   1. Look up the FieldSpec in the contract registry (by device + field name).
//!   2. Translate proto `Value` ↔ internal `TypedValue`.
//!   3. Encode/decode against the corresponding slave's PDO buffer.
//!
//! All semantics — what a field means, what value is valid — lives in the
//! contract YAML and the leaf, not here.

use std::sync::Arc;

use tonic::{Request, Response, Status};
use tracing::debug;

use crate::contract::{ContractRegistry, FieldType};
use crate::ethercat::PdoBuffers;
use crate::translate::{decode_field, encode_field, TypedValue};

/// Generated proto types — included by tonic.
pub mod proto {
    tonic::include_proto!("motion");
}

use proto::motion_service_server::MotionService;
use proto::value::Kind;
use proto::{
    ReadFieldRequest, ReadFieldResponse, Value, WriteFieldRequest, WriteFieldResponse,
};

/// Service implementation. Holds shared references to the registry and buffers.
pub struct MotionServiceImpl {
    pub contracts: Arc<ContractRegistry>,
    pub buffers: Arc<PdoBuffers>,
}

#[tonic::async_trait]
impl MotionService for MotionServiceImpl {
    async fn write_field(
        &self,
        request: Request<WriteFieldRequest>,
    ) -> Result<Response<WriteFieldResponse>, Status> {
        let req = request.into_inner();
        debug!(device = %req.device, field = %req.field, "WriteField");

        match write_field_impl(&self.contracts, &self.buffers, &req) {
            Ok(()) => Ok(Response::new(WriteFieldResponse {
                ok: true,
                error: String::new(),
            })),
            Err(e) => Ok(Response::new(WriteFieldResponse {
                ok: false,
                error: e.to_string(),
            })),
        }
    }

    async fn read_field(
        &self,
        request: Request<ReadFieldRequest>,
    ) -> Result<Response<ReadFieldResponse>, Status> {
        let req = request.into_inner();
        debug!(device = %req.device, field = %req.field, "ReadField");

        match read_field_impl(&self.contracts, &self.buffers, &req) {
            Ok(value) => Ok(Response::new(ReadFieldResponse {
                ok: true,
                error: String::new(),
                value: Some(value),
            })),
            Err(e) => Ok(Response::new(ReadFieldResponse {
                ok: false,
                error: e.to_string(),
                value: None,
            })),
        }
    }
}

// ---------------------------------------------------------------------------
// Internal helpers: separate from the trait impl so they return anyhow::Result
// and are easier to unit-test.
// ---------------------------------------------------------------------------

fn write_field_impl(
    contracts: &ContractRegistry,
    buffers: &PdoBuffers,
    req: &WriteFieldRequest,
) -> anyhow::Result<()> {
    let (entry, spec) = contracts.field(&req.device, &req.field)?;
    let slave_position = entry
        .slave_position
        .ok_or_else(|| anyhow::anyhow!("device {} not bound to any slave", req.device))?;

    // Direction sanity: write_field expects an `out` field.
    if spec.dir != crate::contract::FieldDir::Out {
        anyhow::bail!(
            "field {} on device {} is dir=in; use read_field instead",
            req.field,
            req.device
        );
    }

    let value = req
        .value
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("WriteFieldRequest missing value"))?;
    let typed = proto_value_to_typed(value)?;

    let slave_bufs = buffers.get(slave_position)?;
    let mut rx = slave_bufs.rx.lock().unwrap();
    encode_field(&spec, &typed, &mut rx)?;
    Ok(())
}

fn read_field_impl(
    contracts: &ContractRegistry,
    buffers: &PdoBuffers,
    req: &ReadFieldRequest,
) -> anyhow::Result<Value> {
    let (entry, spec) = contracts.field(&req.device, &req.field)?;
    let slave_position = entry
        .slave_position
        .ok_or_else(|| anyhow::anyhow!("device {} not bound to any slave", req.device))?;

    if spec.dir != crate::contract::FieldDir::In {
        anyhow::bail!(
            "field {} on device {} is dir=out; use write_field instead",
            req.field,
            req.device
        );
    }

    let slave_bufs = buffers.get(slave_position)?;
    let tx = slave_bufs.tx.lock().unwrap();
    let typed = decode_field(&spec, &tx)?;
    Ok(typed_to_proto_value(&typed))
}

// ---------------------------------------------------------------------------
// proto Value ↔ TypedValue conversion
// ---------------------------------------------------------------------------

fn proto_value_to_typed(v: &Value) -> anyhow::Result<TypedValue> {
    match v.kind.as_ref() {
        Some(Kind::VBool(b)) => Ok(TypedValue::Bool(*b)),
        Some(Kind::VI32(i)) => Ok(TypedValue::I32(*i)),
        Some(Kind::VU32(u)) => Ok(TypedValue::U32(*u)),
        Some(Kind::VI64(i)) => Ok(TypedValue::I64(*i)),
        Some(Kind::VU64(u)) => Ok(TypedValue::U64(*u)),
        Some(Kind::VF32(f)) => Ok(TypedValue::F32(*f)),
        Some(Kind::VF64(f)) => Ok(TypedValue::F64(*f)),
        Some(Kind::VBytes(b)) => Ok(TypedValue::Bytes(b.clone())),
        None => anyhow::bail!("Value kind is empty"),
    }
}

fn typed_to_proto_value(v: &TypedValue) -> Value {
    let kind = match v {
        TypedValue::Bool(b) => Kind::VBool(*b),
        // Small ints widen to i32 / u32 on the wire.
        TypedValue::I8(i) => Kind::VI32(*i as i32),
        TypedValue::U8(u) => Kind::VU32(*u as u32),
        TypedValue::I16(i) => Kind::VI32(*i as i32),
        TypedValue::U16(u) => Kind::VU32(*u as u32),
        TypedValue::I32(i) => Kind::VI32(*i),
        TypedValue::U32(u) => Kind::VU32(*u),
        TypedValue::I64(i) => Kind::VI64(*i),
        TypedValue::U64(u) => Kind::VU64(*u),
        TypedValue::F32(f) => Kind::VF32(*f),
        TypedValue::F64(f) => Kind::VF64(*f),
        TypedValue::Bytes(b) => Kind::VBytes(b.clone()),
        TypedValue::Str(s) => Kind::VBytes(s.as_bytes().to_vec()),
    };
    // Silence unused warning for FieldType — keep it imported for future
    // strict type checking between proto Value and field type.
    let _ = std::marker::PhantomData::<FieldType>;
    Value { kind: Some(kind) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::{Contract, FieldDir, FieldSpec, FieldType, PdoMapping, SlaveMatch};

    fn mk_registry() -> (Arc<ContractRegistry>, Arc<PdoBuffers>) {
        let mut fields = std::collections::BTreeMap::new();
        fields.insert(
            "target_x".to_string(),
            FieldSpec {
                offset: 0,
                field_type: FieldType::F32,
                dir: FieldDir::Out,
                bit: None,
            },
        );
        fields.insert(
            "done".to_string(),
            FieldSpec {
                offset: 0,
                field_type: FieldType::Bool,
                dir: FieldDir::In,
                bit: Some(0),
            },
        );

        let contract = Contract {
            device: "arm".into(),
            description: String::new(),
            protocol_version: 1,
            slave_match: SlaveMatch::default(),
            pdo_mapping: PdoMapping {
                rx_pdo_index: 0x1600,
                rx_pdo_size: 32,
                tx_pdo_index: 0x1A00,
                tx_pdo_size: 16,
            },
            fields,
        };

        let mut reg = ContractRegistry::new();
        reg.insert(contract).unwrap();
        reg.bind_slave("arm", 0).unwrap();

        let mut bufs = PdoBuffers::new();
        bufs.insert(0, 32, 16);

        (Arc::new(reg), Arc::new(bufs))
    }

    #[test]
    fn write_field_writes_into_rx_buffer() {
        let (contracts, buffers) = mk_registry();
        let req = WriteFieldRequest {
            device: "arm".into(),
            field: "target_x".into(),
            value: Some(Value {
                kind: Some(Kind::VF32(120.5)),
            }),
        };
        write_field_impl(&contracts, &buffers, &req).unwrap();

        let rx = buffers.get(0).unwrap().rx.lock().unwrap();
        assert_eq!(&rx[0..4], &120.5f32.to_le_bytes());
    }

    #[test]
    fn read_field_reads_from_tx_buffer() {
        let (contracts, buffers) = mk_registry();
        // Simulate the cyclic loop having written TxPDO bit 0 = 1
        buffers.get(0).unwrap().tx.lock().unwrap()[0] = 0b0000_0001;

        let req = ReadFieldRequest {
            device: "arm".into(),
            field: "done".into(),
        };
        let value = read_field_impl(&contracts, &buffers, &req).unwrap();
        assert!(matches!(value.kind, Some(Kind::VBool(true))));
    }

    #[test]
    fn write_to_in_field_rejected() {
        let (contracts, buffers) = mk_registry();
        let req = WriteFieldRequest {
            device: "arm".into(),
            field: "done".into(),
            value: Some(Value {
                kind: Some(Kind::VBool(true)),
            }),
        };
        let err = write_field_impl(&contracts, &buffers, &req).unwrap_err();
        assert!(err.to_string().contains("dir=in"));
    }

    #[test]
    fn read_from_out_field_rejected() {
        let (contracts, buffers) = mk_registry();
        let req = ReadFieldRequest {
            device: "arm".into(),
            field: "target_x".into(),
        };
        let err = read_field_impl(&contracts, &buffers, &req).unwrap_err();
        assert!(err.to_string().contains("dir=out"));
    }
}
