//! gRPC server implementing the 0.8.0 goal-service surface.
//!
//! Four RPCs total:
//!
//!   SubmitScaraGoal / ReadScaraStatus — full LS6 SCARA support
//!   SubmitArm6Goal  / ReadArm6Status  — stub, returns Unimplemented
//!
//! The shell is thin: each RPC translates the proto message into the
//! corresponding `goal` module call. The handshake (write fields, raise
//! trigger, drop trigger on done) lives in `goal::scara`.

use std::sync::Arc;

use tonic::{Request, Response, Status};
use tracing::debug;

use crate::contract::ContractRegistry;
use crate::ethercat::PdoBuffers;
use crate::goal::{self, ScaraGoalArgs};

/// Generated proto types — included by tonic.
pub mod proto {
    tonic::include_proto!("motion");
}

use proto::motion_service_server::MotionService;
use proto::{
    Arm6Goal, Arm6StatusResponse, GoalResponse, Motion4, ScaraGoal, ScaraStatusResponse,
    StatusRequest,
};

/// Service implementation. Holds shared references to the registry and buffers.
pub struct MotionServiceImpl {
    pub contracts: Arc<ContractRegistry>,
    pub buffers: Arc<PdoBuffers>,
}

#[tonic::async_trait]
impl MotionService for MotionServiceImpl {
    async fn submit_scara_goal(
        &self,
        request: Request<ScaraGoal>,
    ) -> Result<Response<GoalResponse>, Status> {
        let req = request.into_inner();
        debug!(
            device = %req.device,
            motion = ?req.motion,
            x = req.x, y = req.y, z = req.z, u = req.u,
            speed = req.speed, accel = req.accel,
            "SubmitScaraGoal"
        );

        let motion_name = match motion4_name(req.motion) {
            Some(name) => name,
            None => {
                return Ok(Response::new(GoalResponse {
                    ok: false,
                    error: format!("invalid Motion4 value {} (UNSPECIFIED is not a valid motion)", req.motion),
                }));
            }
        };

        let args = ScaraGoalArgs {
            device: req.device,
            motion: motion_name.into(),
            x: req.x,
            y: req.y,
            z: req.z,
            u: req.u,
            speed: req.speed as u16,
            accel: req.accel as u16,
        };

        match goal::submit_scara_goal(&self.contracts, &self.buffers, &args) {
            Ok(()) => Ok(Response::new(GoalResponse {
                ok: true,
                error: String::new(),
            })),
            Err(e) => Ok(Response::new(GoalResponse {
                ok: false,
                error: e.to_string(),
            })),
        }
    }

    async fn read_scara_status(
        &self,
        request: Request<StatusRequest>,
    ) -> Result<Response<ScaraStatusResponse>, Status> {
        let req = request.into_inner();
        debug!(device = %req.device, "ReadScaraStatus");

        match goal::read_scara_status(&self.contracts, &self.buffers, &req.device) {
            Ok(status) => Ok(Response::new(ScaraStatusResponse {
                ok: true,
                error: String::new(),
                done: status.done,
                busy: status.busy,
                error_code: status.error_code as u32,
                current_x: status.current_x,
                current_y: status.current_y,
                current_z: status.current_z,
                current_u: status.current_u,
                joint_1: status.joint_1,
                joint_2: status.joint_2,
                joint_3: status.joint_3,
                joint_4: status.joint_4,
            })),
            Err(e) => Ok(Response::new(ScaraStatusResponse {
                ok: false,
                error: e.to_string(),
                ..Default::default()
            })),
        }
    }

    async fn submit_arm6_goal(
        &self,
        _request: Request<Arm6Goal>,
    ) -> Result<Response<GoalResponse>, Status> {
        // 0.8.0 has no 6-DOF EtherCAT-fronted arm. Returning Unimplemented
        // is intentional — when one is integrated we'll plumb it through
        // `goal::arm6` and remove this stub.
        Err(Status::unimplemented(
            "Arm6 goal service not implemented in 0.8.0 — no 6-DOF EtherCAT device yet",
        ))
    }

    async fn read_arm6_status(
        &self,
        _request: Request<StatusRequest>,
    ) -> Result<Response<Arm6StatusResponse>, Status> {
        Err(Status::unimplemented(
            "Arm6 status not implemented in 0.8.0 — no 6-DOF EtherCAT device yet",
        ))
    }
}

/// Translate the proto Motion4 i32 wire value into the routine-table
/// lookup key. Returns None for UNSPECIFIED or any unknown value.
fn motion4_name(motion: i32) -> Option<&'static str> {
    match Motion4::try_from(motion).ok()? {
        Motion4::Unspecified => None,
        Motion4::Go => Some("GO"),
        Motion4::Jump => Some("JUMP"),
        Motion4::Linear => Some("LINEAR"),
        Motion4::Home => Some("HOME"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::{Contract, FieldDir, FieldSpec, FieldType, PdoMapping, SlaveMatch};

    /// Build a minimal LS6-shaped registry + buffers. Same layout as
    /// `goal::scara::tests::mk_fixture` but local so we don't expose it.
    fn mk_registry() -> (Arc<ContractRegistry>, Arc<PdoBuffers>) {
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
        fields.insert("target_x".into(), f32_out(0));
        fields.insert("target_y".into(), f32_out(4));
        fields.insert("target_z".into(), f32_out(8));
        fields.insert("target_u".into(), f32_out(12));
        fields.insert(
            "speed".into(),
            FieldSpec { offset: 16, field_type: FieldType::U16, dir: FieldDir::Out, bit: None },
        );
        fields.insert(
            "accel".into(),
            FieldSpec { offset: 18, field_type: FieldType::U16, dir: FieldDir::Out, bit: None },
        );
        fields.insert(
            "routine".into(),
            FieldSpec { offset: 20, field_type: FieldType::U8, dir: FieldDir::Out, bit: None },
        );
        fields.insert(
            "trigger".into(),
            FieldSpec { offset: 22, field_type: FieldType::Bool, dir: FieldDir::Out, bit: Some(0) },
        );
        fields.insert(
            "done".into(),
            FieldSpec { offset: 0, field_type: FieldType::Bool, dir: FieldDir::In, bit: Some(0) },
        );
        fields.insert(
            "busy".into(),
            FieldSpec { offset: 0, field_type: FieldType::Bool, dir: FieldDir::In, bit: Some(1) },
        );
        fields.insert(
            "error_code".into(),
            FieldSpec { offset: 2, field_type: FieldType::U16, dir: FieldDir::In, bit: None },
        );
        fields.insert("current_x".into(), f32_in(4));
        fields.insert("current_y".into(), f32_in(8));
        fields.insert("current_z".into(), f32_in(12));
        fields.insert("current_u".into(), f32_in(16));
        fields.insert("joint_1".into(), f32_in(20));
        fields.insert("joint_2".into(), f32_in(24));
        fields.insert("joint_3".into(), f32_in(28));
        fields.insert("joint_4".into(), f32_in(32));

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

        let mut reg = ContractRegistry::new();
        reg.insert(contract).unwrap();
        reg.bind_slave("ls6", 0).unwrap();

        let mut bufs = PdoBuffers::new();
        bufs.insert(0, 32, 36);

        (Arc::new(reg), Arc::new(bufs))
    }

    #[tokio::test]
    async fn submit_scara_writes_fields_and_returns_ok() {
        let (contracts, buffers) = mk_registry();
        let svc = MotionServiceImpl {
            contracts: Arc::clone(&contracts),
            buffers: Arc::clone(&buffers),
        };
        let req = Request::new(ScaraGoal {
            device: "ls6".into(),
            motion: Motion4::Linear as i32,
            x: 1.0,
            y: 2.0,
            z: 3.0,
            u: 4.0,
            speed: 50,
            accel: 200,
        });
        let resp = svc.submit_scara_goal(req).await.unwrap().into_inner();
        assert!(resp.ok);
        assert_eq!(resp.error, "");

        let rx = buffers.get(0).unwrap().rx.lock().unwrap();
        assert_eq!(&rx[0..4], &1.0f32.to_le_bytes());
        assert_eq!(rx[20], 3); // LINEAR
        assert_eq!(rx[22] & 0x01, 0x01); // trigger raised
    }

    #[tokio::test]
    async fn submit_scara_unspecified_motion_returns_error() {
        let (contracts, buffers) = mk_registry();
        let svc = MotionServiceImpl { contracts, buffers };
        let req = Request::new(ScaraGoal {
            device: "ls6".into(),
            motion: Motion4::Unspecified as i32,
            x: 0.0, y: 0.0, z: 0.0, u: 0.0,
            speed: 1, accel: 1,
        });
        let resp = svc.submit_scara_goal(req).await.unwrap().into_inner();
        assert!(!resp.ok);
        assert!(resp.error.contains("UNSPECIFIED"));
    }

    #[tokio::test]
    async fn submit_scara_unknown_device_returns_error_response() {
        let (contracts, buffers) = mk_registry();
        let svc = MotionServiceImpl { contracts, buffers };
        let req = Request::new(ScaraGoal {
            device: "ghost".into(),
            motion: Motion4::Linear as i32,
            x: 0.0, y: 0.0, z: 0.0, u: 0.0,
            speed: 1, accel: 1,
        });
        let resp = svc.submit_scara_goal(req).await.unwrap().into_inner();
        assert!(!resp.ok);
        assert!(resp.error.contains("ghost"));
    }

    #[tokio::test]
    async fn read_scara_status_returns_decoded_fields() {
        let (contracts, buffers) = mk_registry();
        // Seed TxPDO: done=1, busy=0, current_x = 100.0
        {
            let mut tx = buffers.get(0).unwrap().tx.lock().unwrap();
            tx[0] = 0b0000_0001;
            tx[4..8].copy_from_slice(&100.0f32.to_le_bytes());
        }
        let svc = MotionServiceImpl { contracts, buffers };
        let req = Request::new(StatusRequest { device: "ls6".into() });
        let resp = svc.read_scara_status(req).await.unwrap().into_inner();
        assert!(resp.ok);
        assert!(resp.done);
        assert_eq!(resp.current_x, 100.0);
    }

    #[tokio::test]
    async fn read_scara_status_unknown_device_returns_error_response() {
        let (contracts, buffers) = mk_registry();
        let svc = MotionServiceImpl { contracts, buffers };
        let req = Request::new(StatusRequest { device: "ghost".into() });
        let resp = svc.read_scara_status(req).await.unwrap().into_inner();
        assert!(!resp.ok);
        assert!(resp.error.contains("ghost"));
    }

    #[tokio::test]
    async fn arm6_rpcs_return_unimplemented() {
        let (contracts, buffers) = mk_registry();
        let svc = MotionServiceImpl { contracts, buffers };
        let err = svc
            .submit_arm6_goal(Request::new(Arm6Goal::default()))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unimplemented);
    }
}
