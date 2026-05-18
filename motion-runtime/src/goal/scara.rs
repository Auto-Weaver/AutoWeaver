//! SCARA goal service: submit + status, with built-in trigger edge handshake.
//!
//! The translate module already knows how to write any single field into a
//! byte buffer; this module orchestrates **which** fields, in what order,
//! for one motion submission. The order is fixed for the LS6 handshake:
//!
//!   1. Stage all target / motion-parameter fields into the RxPDO buffer.
//!   2. Compute the routine number from the motion enum (via contract's
//!      `motion_routines` table) and write that to the `routine` field.
//!   3. Flip `trigger` 0→1 (the rising edge). SPEL+ now starts the motion.
//!
//! All writes happen under one RxPDO lock so the EtherCAT cyclic loop can
//! never see a half-staged frame.
//!
//! The matching `read_scara_status` reads TxPDO once, returns the structured
//! status, and as a side effect drops trigger to 0 if motion has completed.
//! See `trigger` module docs for the rationale.

use std::sync::Arc;

use anyhow::{anyhow, Context, Result};

use crate::contract::{ContractRegistry, FieldDir};
use crate::ethercat::PdoBuffers;
use crate::translate::{decode_field, encode_field, TypedValue};

/// Field-name catalog for the SCARA contract. Centralized so a typo in one
/// place doesn't silently miss a write.
mod field_names {
    pub const TARGET_X: &str = "target_x";
    pub const TARGET_Y: &str = "target_y";
    pub const TARGET_Z: &str = "target_z";
    pub const TARGET_U: &str = "target_u";
    pub const SPEED: &str = "speed";
    pub const ACCEL: &str = "accel";
    pub const ROUTINE: &str = "routine";
    pub const TRIGGER: &str = "trigger";

    pub const DONE: &str = "done";
    pub const BUSY: &str = "busy";
    pub const ERROR_CODE: &str = "error_code";
    pub const CURRENT_X: &str = "current_x";
    pub const CURRENT_Y: &str = "current_y";
    pub const CURRENT_Z: &str = "current_z";
    pub const CURRENT_U: &str = "current_u";
    pub const JOINT_1: &str = "joint_1";
    pub const JOINT_2: &str = "joint_2";
    pub const JOINT_3: &str = "joint_3";
    pub const JOINT_4: &str = "joint_4";
}

/// Plain-data view of a `SubmitScaraGoal` request. The gRPC layer converts
/// the proto message into this struct so this module doesn't depend on
/// generated types.
#[derive(Debug, Clone)]
pub struct ScaraGoalArgs {
    pub device: String,
    /// Motion enum name without prefix (e.g. `"LINEAR"`, `"GO"`, `"JUMP"`,
    /// `"HOME"`). Looked up in the contract's `motion_routines` table.
    pub motion: String,
    pub x: f32,
    pub y: f32,
    pub z: f32,
    pub u: f32,
    pub speed: u16,
    pub accel: u16,
}

/// Decoded TxPDO status snapshot.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ScaraStatus {
    pub done: bool,
    pub busy: bool,
    pub error_code: u16,
    pub current_x: f32,
    pub current_y: f32,
    pub current_z: f32,
    pub current_u: f32,
    pub joint_1: f32,
    pub joint_2: f32,
    pub joint_3: f32,
    pub joint_4: f32,
}

/// Stage the goal's fields into the device's RxPDO buffer and raise the
/// trigger 0→1 to start the handshake. Returns immediately; the caller is
/// expected to poll `read_scara_status` for completion.
pub fn submit_scara_goal(
    contracts: &Arc<ContractRegistry>,
    buffers: &Arc<PdoBuffers>,
    args: &ScaraGoalArgs,
) -> Result<()> {
    let slave_position = resolve_slave(contracts, &args.device)?;
    let routine = contracts
        .motion_routine(&args.device, &args.motion)
        .with_context(|| format!("resolving motion {}", args.motion))?;

    let slave_bufs = buffers.get(slave_position)?;
    let mut rx = slave_bufs.rx.lock().unwrap();

    // 1) Stage target + motion params.
    write_named_field(
        contracts,
        &args.device,
        field_names::TARGET_X,
        TypedValue::F32(args.x),
        &mut rx,
    )?;
    write_named_field(
        contracts,
        &args.device,
        field_names::TARGET_Y,
        TypedValue::F32(args.y),
        &mut rx,
    )?;
    write_named_field(
        contracts,
        &args.device,
        field_names::TARGET_Z,
        TypedValue::F32(args.z),
        &mut rx,
    )?;
    write_named_field(
        contracts,
        &args.device,
        field_names::TARGET_U,
        TypedValue::F32(args.u),
        &mut rx,
    )?;
    write_named_field(
        contracts,
        &args.device,
        field_names::SPEED,
        TypedValue::U16(args.speed),
        &mut rx,
    )?;
    write_named_field(
        contracts,
        &args.device,
        field_names::ACCEL,
        TypedValue::U16(args.accel),
        &mut rx,
    )?;
    write_named_field(
        contracts,
        &args.device,
        field_names::ROUTINE,
        TypedValue::U8(routine),
        &mut rx,
    )?;

    // 2) Rising edge — write trigger LAST so SPEL+ only sees a complete
    //    field set when it unblocks from Wait Sw(IN_TRIGGER)=1.
    write_named_field(
        contracts,
        &args.device,
        field_names::TRIGGER,
        TypedValue::Bool(true),
        &mut rx,
    )?;

    Ok(())
}

/// Read the TxPDO once and decode all status fields. If the motion has
/// finished (done=true) and the trigger is still 1 from this submission,
/// flip it back to 0 (the falling edge) so SPEL+'s `Wait Sw(IN_TRIGGER)=0`
/// can unblock and the next submission gets a real rising edge.
///
/// See EVO-003 "Trigger 边沿协议在 goal 服务内的实现" for the design rationale.
pub fn read_scara_status(
    contracts: &Arc<ContractRegistry>,
    buffers: &Arc<PdoBuffers>,
    device: &str,
) -> Result<ScaraStatus> {
    let slave_position = resolve_slave(contracts, device)?;
    let slave_bufs = buffers.get(slave_position)?;

    // Read TxPDO snapshot. The cyclic loop may rewrite this buffer at any
    // moment, but each individual field decode reads contiguous bytes
    // under the lock, so a single field is never torn. Inter-field tear
    // is possible (e.g. read x before, y after a cycle) — for status this
    // is acceptable; pose drift between sequential reads is sub-millimeter.
    let status = {
        let tx = slave_bufs.tx.lock().unwrap();
        decode_status(contracts, device, &tx)?
    };

    // Piggyback the trigger falling edge: if SPEL+ is done and our trigger
    // is still high, drop it now so SPEL+ unblocks from
    // Wait Sw(IN_TRIGGER)=0 and is ready for the next motion.
    if status.done {
        let mut rx = slave_bufs.rx.lock().unwrap();
        let current_trigger = read_trigger_state(contracts, device, &rx)?;
        if current_trigger {
            write_named_field(
                contracts,
                device,
                field_names::TRIGGER,
                TypedValue::Bool(false),
                &mut rx,
            )?;
        }
    }

    Ok(status)
}

// ─── internals ─────────────────────────────────────────────────────────────

/// Look up the slave position bound to a device.
fn resolve_slave(contracts: &ContractRegistry, device: &str) -> Result<u16> {
    let entry = contracts.get(device)?;
    entry
        .slave_position
        .ok_or_else(|| anyhow!("device {} not bound to any slave", device))
}

/// Look up a field's spec and write a value into the buffer.
/// The buffer must be the RxPDO buffer for the device's slave; we check
/// `dir == Out` so a stray TxPDO-direction lookup fails loudly.
fn write_named_field(
    contracts: &ContractRegistry,
    device: &str,
    field: &str,
    value: TypedValue,
    rx: &mut [u8],
) -> Result<()> {
    let (_, spec) = contracts.field(device, field)?;
    if spec.dir != FieldDir::Out {
        anyhow::bail!(
            "field {} on device {} is dir=in, expected dir=out for staging",
            field,
            device
        );
    }
    encode_field(&spec, &value, rx)
        .with_context(|| format!("encoding field {} on device {}", field, device))?;
    Ok(())
}

/// Read the current value of the trigger bit from our own RxPDO staging
/// buffer. The cyclic loop hasn't yet pushed this to the wire necessarily,
/// but that's fine — we care about "have *we* already raised it" not "has
/// the slave seen it".
fn read_trigger_state(
    contracts: &ContractRegistry,
    device: &str,
    rx: &[u8],
) -> Result<bool> {
    let (_, spec) = contracts.field(device, field_names::TRIGGER)?;
    let decoded = decode_field(&spec, rx)?;
    match decoded {
        TypedValue::Bool(b) => Ok(b),
        other => anyhow::bail!(
            "trigger field on device {} decoded as non-bool: {:?}",
            device,
            other
        ),
    }
}

/// Decode the full status block from a TxPDO snapshot.
fn decode_status(
    contracts: &ContractRegistry,
    device: &str,
    tx: &[u8],
) -> Result<ScaraStatus> {
    Ok(ScaraStatus {
        done: read_bool(contracts, device, field_names::DONE, tx)?,
        busy: read_bool(contracts, device, field_names::BUSY, tx)?,
        error_code: read_u16(contracts, device, field_names::ERROR_CODE, tx)?,
        current_x: read_f32(contracts, device, field_names::CURRENT_X, tx)?,
        current_y: read_f32(contracts, device, field_names::CURRENT_Y, tx)?,
        current_z: read_f32(contracts, device, field_names::CURRENT_Z, tx)?,
        current_u: read_f32(contracts, device, field_names::CURRENT_U, tx)?,
        joint_1: read_f32(contracts, device, field_names::JOINT_1, tx)?,
        joint_2: read_f32(contracts, device, field_names::JOINT_2, tx)?,
        joint_3: read_f32(contracts, device, field_names::JOINT_3, tx)?,
        joint_4: read_f32(contracts, device, field_names::JOINT_4, tx)?,
    })
}

fn read_bool(
    contracts: &ContractRegistry,
    device: &str,
    field: &str,
    tx: &[u8],
) -> Result<bool> {
    let (_, spec) = contracts.field(device, field)?;
    match decode_field(&spec, tx)? {
        TypedValue::Bool(b) => Ok(b),
        other => anyhow::bail!(
            "field {} on {} expected bool, got {:?}",
            field,
            device,
            other
        ),
    }
}

fn read_u16(
    contracts: &ContractRegistry,
    device: &str,
    field: &str,
    tx: &[u8],
) -> Result<u16> {
    let (_, spec) = contracts.field(device, field)?;
    match decode_field(&spec, tx)? {
        TypedValue::U16(v) => Ok(v),
        TypedValue::U32(v) => Ok(v as u16),
        other => anyhow::bail!(
            "field {} on {} expected u16-like, got {:?}",
            field,
            device,
            other
        ),
    }
}

fn read_f32(
    contracts: &ContractRegistry,
    device: &str,
    field: &str,
    tx: &[u8],
) -> Result<f32> {
    let (_, spec) = contracts.field(device, field)?;
    match decode_field(&spec, tx)? {
        TypedValue::F32(v) => Ok(v),
        other => anyhow::bail!(
            "field {} on {} expected f32, got {:?}",
            field,
            device,
            other
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::{Contract, FieldDir, FieldSpec, FieldType, PdoMapping, SlaveMatch};

    /// Build a minimal LS6-like contract + bound slave + buffers for tests.
    fn mk_fixture() -> (Arc<ContractRegistry>, Arc<PdoBuffers>) {
        // RxPDO layout: target_x(0..4) y(4..8) z(8..12) u(12..16)
        //               speed(16..18) accel(18..20)
        //               routine(20..21) trigger(byte 22 bit 0)
        let mut fields = std::collections::BTreeMap::new();
        let f32_out = |off: usize| FieldSpec {
            offset: off,
            field_type: FieldType::F32,
            dir: FieldDir::Out,
            bit: None,
        };
        let f32_in = |off: usize| FieldSpec {
            offset: off,
            field_type: FieldType::F32,
            dir: FieldDir::In,
            bit: None,
        };
        fields.insert(field_names::TARGET_X.into(), f32_out(0));
        fields.insert(field_names::TARGET_Y.into(), f32_out(4));
        fields.insert(field_names::TARGET_Z.into(), f32_out(8));
        fields.insert(field_names::TARGET_U.into(), f32_out(12));
        fields.insert(
            field_names::SPEED.into(),
            FieldSpec { offset: 16, field_type: FieldType::U16, dir: FieldDir::Out, bit: None },
        );
        fields.insert(
            field_names::ACCEL.into(),
            FieldSpec { offset: 18, field_type: FieldType::U16, dir: FieldDir::Out, bit: None },
        );
        fields.insert(
            field_names::ROUTINE.into(),
            FieldSpec { offset: 20, field_type: FieldType::U8, dir: FieldDir::Out, bit: None },
        );
        fields.insert(
            field_names::TRIGGER.into(),
            FieldSpec { offset: 22, field_type: FieldType::Bool, dir: FieldDir::Out, bit: Some(0) },
        );

        // TxPDO layout: done/busy at byte 0 bit 0/1, error_code at byte 2-3,
        //               pose at 4..20, joints at 20..36
        fields.insert(
            field_names::DONE.into(),
            FieldSpec { offset: 0, field_type: FieldType::Bool, dir: FieldDir::In, bit: Some(0) },
        );
        fields.insert(
            field_names::BUSY.into(),
            FieldSpec { offset: 0, field_type: FieldType::Bool, dir: FieldDir::In, bit: Some(1) },
        );
        fields.insert(
            field_names::ERROR_CODE.into(),
            FieldSpec { offset: 2, field_type: FieldType::U16, dir: FieldDir::In, bit: None },
        );
        fields.insert(field_names::CURRENT_X.into(), f32_in(4));
        fields.insert(field_names::CURRENT_Y.into(), f32_in(8));
        fields.insert(field_names::CURRENT_Z.into(), f32_in(12));
        fields.insert(field_names::CURRENT_U.into(), f32_in(16));
        fields.insert(field_names::JOINT_1.into(), f32_in(20));
        fields.insert(field_names::JOINT_2.into(), f32_in(24));
        fields.insert(field_names::JOINT_3.into(), f32_in(28));
        fields.insert(field_names::JOINT_4.into(), f32_in(32));

        let mut motion_routines = std::collections::BTreeMap::new();
        motion_routines.insert("GO".into(), 1);
        motion_routines.insert("JUMP".into(), 2);
        motion_routines.insert("LINEAR".into(), 3);
        motion_routines.insert("HOME".into(), 4);

        let contract = Contract {
            device: "ls6".into(),
            description: String::new(),
            protocol_version: 3,
            slave_match: SlaveMatch::default(),
            pdo_mapping: PdoMapping {
                rx_pdo_index: 0x1600,
                rx_pdo_size: 32,
                tx_pdo_index: 0x1A00,
                tx_pdo_size: 36,
            },
            fields,
            motion_routines,
        };

        let mut registry = ContractRegistry::new();
        registry.insert(contract).unwrap();
        registry.bind_slave("ls6", 0).unwrap();

        let mut buffers = PdoBuffers::new();
        buffers.insert(0, 32, 36);

        (Arc::new(registry), Arc::new(buffers))
    }

    fn rx_bytes(buffers: &PdoBuffers) -> Vec<u8> {
        buffers.get(0).unwrap().rx.lock().unwrap().clone()
    }

    fn set_tx_bytes(buffers: &PdoBuffers, writer: impl FnOnce(&mut [u8])) {
        let mut tx = buffers.get(0).unwrap().tx.lock().unwrap();
        writer(&mut tx);
    }

    // ─── submit_scara_goal ─────────────────────────────────────────────

    #[test]
    fn submit_writes_target_speed_accel_routine_trigger() {
        let (contracts, buffers) = mk_fixture();
        submit_scara_goal(
            &contracts,
            &buffers,
            &ScaraGoalArgs {
                device: "ls6".into(),
                motion: "LINEAR".into(),
                x: 100.5,
                y: 200.0,
                z: 50.0,
                u: 90.0,
                speed: 50,
                accel: 200,
            },
        )
        .unwrap();

        let rx = rx_bytes(&buffers);
        assert_eq!(&rx[0..4], &100.5f32.to_le_bytes());
        assert_eq!(&rx[4..8], &200.0f32.to_le_bytes());
        assert_eq!(&rx[8..12], &50.0f32.to_le_bytes());
        assert_eq!(&rx[12..16], &90.0f32.to_le_bytes());
        assert_eq!(&rx[16..18], &50u16.to_le_bytes());
        assert_eq!(&rx[18..20], &200u16.to_le_bytes());
        assert_eq!(rx[20], 3); // LINEAR → routine 3
        assert_eq!(rx[22] & 0x01, 0x01); // trigger bit 0 raised
    }

    #[test]
    fn submit_resolves_motion_to_routine_number() {
        let (contracts, buffers) = mk_fixture();
        for (motion, expected) in [("GO", 1), ("JUMP", 2), ("LINEAR", 3), ("HOME", 4)] {
            // Reset buffer between attempts.
            buffers.get(0).unwrap().rx.lock().unwrap().fill(0);
            submit_scara_goal(
                &contracts,
                &buffers,
                &ScaraGoalArgs {
                    device: "ls6".into(),
                    motion: motion.into(),
                    x: 0.0,
                    y: 0.0,
                    z: 0.0,
                    u: 0.0,
                    speed: 1,
                    accel: 1,
                },
            )
            .unwrap();
            assert_eq!(rx_bytes(&buffers)[20], expected, "{} → {}", motion, expected);
        }
    }

    #[test]
    fn submit_unknown_motion_errors() {
        let (contracts, buffers) = mk_fixture();
        let err = submit_scara_goal(
            &contracts,
            &buffers,
            &ScaraGoalArgs {
                device: "ls6".into(),
                motion: "PIROUETTE".into(),
                x: 0.0,
                y: 0.0,
                z: 0.0,
                u: 0.0,
                speed: 1,
                accel: 1,
            },
        )
        .unwrap_err();
        assert!(err.to_string().contains("PIROUETTE"));
    }

    // ─── read_scara_status ─────────────────────────────────────────────

    #[test]
    fn read_status_decodes_all_fields() {
        let (contracts, buffers) = mk_fixture();
        set_tx_bytes(&buffers, |tx| {
            tx[0] = 0b0000_0001; // done bit 0 = 1, busy bit 1 = 0
            tx[2..4].copy_from_slice(&0u16.to_le_bytes()); // error_code = 0
            tx[4..8].copy_from_slice(&123.0f32.to_le_bytes()); // current_x
            tx[8..12].copy_from_slice(&234.0f32.to_le_bytes()); // current_y
            tx[12..16].copy_from_slice(&345.0f32.to_le_bytes()); // current_z
            tx[16..20].copy_from_slice(&90.0f32.to_le_bytes()); // current_u
            tx[20..24].copy_from_slice(&1.0f32.to_le_bytes());
            tx[24..28].copy_from_slice(&2.0f32.to_le_bytes());
            tx[28..32].copy_from_slice(&3.0f32.to_le_bytes());
            tx[32..36].copy_from_slice(&4.0f32.to_le_bytes());
        });

        let status = read_scara_status(&contracts, &buffers, "ls6").unwrap();
        assert!(status.done);
        assert!(!status.busy);
        assert_eq!(status.error_code, 0);
        assert_eq!(status.current_x, 123.0);
        assert_eq!(status.current_y, 234.0);
        assert_eq!(status.current_z, 345.0);
        assert_eq!(status.current_u, 90.0);
        assert_eq!(status.joint_1, 1.0);
        assert_eq!(status.joint_4, 4.0);
    }

    // ─── trigger falling-edge piggyback ────────────────────────────────

    #[test]
    fn trigger_falls_after_done_is_observed() {
        let (contracts, buffers) = mk_fixture();
        // Submit raises trigger.
        submit_scara_goal(
            &contracts,
            &buffers,
            &ScaraGoalArgs {
                device: "ls6".into(),
                motion: "LINEAR".into(),
                x: 1.0,
                y: 0.0,
                z: 0.0,
                u: 0.0,
                speed: 1,
                accel: 1,
            },
        )
        .unwrap();
        assert_eq!(rx_bytes(&buffers)[22] & 0x01, 0x01);

        // Status reads while busy = true, done = false → trigger stays 1.
        set_tx_bytes(&buffers, |tx| {
            tx[0] = 0b0000_0010; // busy = 1, done = 0
        });
        read_scara_status(&contracts, &buffers, "ls6").unwrap();
        assert_eq!(
            rx_bytes(&buffers)[22] & 0x01,
            0x01,
            "trigger must stay high while motion is in progress"
        );

        // SPEL+ finishes → done flips. Next read_scara_status drops trigger.
        set_tx_bytes(&buffers, |tx| {
            tx[0] = 0b0000_0001; // done = 1, busy = 0
        });
        let status = read_scara_status(&contracts, &buffers, "ls6").unwrap();
        assert!(status.done);
        assert_eq!(
            rx_bytes(&buffers)[22] & 0x01,
            0x00,
            "trigger must fall once done is observed"
        );
    }

    #[test]
    fn trigger_already_low_is_not_redundantly_written() {
        // If trigger is already 0 (e.g. an earlier read_scara_status
        // already piggybacked the falling edge), subsequent reads with
        // done still 1 must just return status — no extra writes needed.
        let (contracts, buffers) = mk_fixture();
        set_tx_bytes(&buffers, |tx| {
            tx[0] = 0b0000_0001; // done = 1
        });
        // First call drops trigger (it was 0 by default — but raise to
        // simulate "submit then immediately done").
        {
            let mut rx = buffers.get(0).unwrap().rx.lock().unwrap();
            rx[22] |= 0x01;
        }
        read_scara_status(&contracts, &buffers, "ls6").unwrap();
        assert_eq!(rx_bytes(&buffers)[22] & 0x01, 0x00);

        // Second call: trigger is already 0, no panic, just status.
        let status = read_scara_status(&contracts, &buffers, "ls6").unwrap();
        assert!(status.done);
    }

    #[test]
    fn next_submit_re_raises_trigger_for_real_rising_edge() {
        let (contracts, buffers) = mk_fixture();

        // Submission 1 → trigger high.
        submit_scara_goal(
            &contracts,
            &buffers,
            &ScaraGoalArgs {
                device: "ls6".into(),
                motion: "LINEAR".into(),
                x: 1.0,
                y: 0.0,
                z: 0.0,
                u: 0.0,
                speed: 1,
                accel: 1,
            },
        )
        .unwrap();
        // SPEL+ finishes.
        set_tx_bytes(&buffers, |tx| tx[0] = 0b0000_0001);
        // Read → trigger falls.
        read_scara_status(&contracts, &buffers, "ls6").unwrap();
        assert_eq!(rx_bytes(&buffers)[22] & 0x01, 0x00);

        // Submission 2 → trigger high again (a real 0→1 edge from SPEL+'s
        // perspective, which is the entire point of dropping it).
        submit_scara_goal(
            &contracts,
            &buffers,
            &ScaraGoalArgs {
                device: "ls6".into(),
                motion: "GO".into(),
                x: 2.0,
                y: 0.0,
                z: 0.0,
                u: 0.0,
                speed: 1,
                accel: 1,
            },
        )
        .unwrap();
        assert_eq!(rx_bytes(&buffers)[22] & 0x01, 0x01);
    }

    #[test]
    fn unknown_device_errors() {
        let (contracts, buffers) = mk_fixture();
        let err = submit_scara_goal(
            &contracts,
            &buffers,
            &ScaraGoalArgs {
                device: "ghost".into(),
                motion: "LINEAR".into(),
                x: 0.0,
                y: 0.0,
                z: 0.0,
                u: 0.0,
                speed: 1,
                accel: 1,
            },
        )
        .unwrap_err();
        assert!(err.to_string().contains("ghost"));
    }
}
