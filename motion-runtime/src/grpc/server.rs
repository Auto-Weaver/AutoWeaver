use std::sync::Arc;

use tokio::sync::Mutex;
use tonic::{Request, Response, Status};
use tracing::info;

use crate::device::manager::DeviceManager;
use crate::device::types::{IoCommand, MotionGoal};

/// Generated proto types — included by tonic.
pub mod proto {
    tonic::include_proto!("motion");
}

use proto::motion_service_server::MotionService;
use proto::{
    FeedbackRequest, FeedbackResponse, GetDigitalInputRequest, GetDigitalInputResponse,
    GoalRequest, GoalResponse, HaltRequest, HaltResponse, ResultRequest, ResultResponse,
    SetDigitalOutputRequest, SetDigitalOutputResponse,
};

/// gRPC service implementation that delegates to the [`DeviceManager`].
pub struct MotionServiceImpl {
    pub manager: Arc<Mutex<DeviceManager>>,
}

#[tonic::async_trait]
impl MotionService for MotionServiceImpl {
    async fn send_goal(
        &self,
        request: Request<GoalRequest>,
    ) -> Result<Response<GoalResponse>, Status> {
        let req = request.into_inner();
        info!(
            axis = req.axis_id,
            target = req.target_position,
            vel = req.velocity,
            "gRPC SendGoal"
        );

        let goal = MotionGoal {
            axis_id: req.axis_id,
            target_position: req.target_position,
            velocity: req.velocity,
            timeout_secs: req.timeout_secs,
        };

        let mut mgr = self.manager.lock().await;
        match mgr.send_goal(goal) {
            Ok(()) => Ok(Response::new(GoalResponse {
                accepted: true,
                message: "Goal accepted".into(),
            })),
            Err(e) => Ok(Response::new(GoalResponse {
                accepted: false,
                message: e.to_string(),
            })),
        }
    }

    async fn get_feedback(
        &self,
        request: Request<FeedbackRequest>,
    ) -> Result<Response<FeedbackResponse>, Status> {
        let req = request.into_inner();
        let mgr = self.manager.lock().await;

        match mgr.get_feedback(req.axis_id) {
            Ok(fb) => Ok(Response::new(FeedbackResponse {
                axis_id: fb.axis_id,
                current_position: fb.current_position,
                state: fb.state,
                progress_pct: fb.progress_pct,
            })),
            Err(e) => Err(Status::not_found(e.to_string())),
        }
    }

    async fn get_result(
        &self,
        request: Request<ResultRequest>,
    ) -> Result<Response<ResultResponse>, Status> {
        let req = request.into_inner();
        let mgr = self.manager.lock().await;

        match mgr.get_result(req.axis_id) {
            Ok(Some(res)) => Ok(Response::new(ResultResponse {
                axis_id: res.axis_id,
                success: res.success,
                final_position: res.final_position,
                error_code: res.error_code,
                error_message: res.error_msg.clone(),
            })),
            Ok(None) => Err(Status::not_found(format!(
                "No result available for axis {}",
                req.axis_id
            ))),
            Err(e) => Err(Status::not_found(e.to_string())),
        }
    }

    async fn halt(
        &self,
        request: Request<HaltRequest>,
    ) -> Result<Response<HaltResponse>, Status> {
        let req = request.into_inner();
        info!(axis = req.axis_id, "gRPC Halt");

        let mgr = self.manager.lock().await;
        match mgr.halt(req.axis_id) {
            Ok(()) => Ok(Response::new(HaltResponse {
                success: true,
                message: "Halt sent".into(),
            })),
            Err(e) => Ok(Response::new(HaltResponse {
                success: false,
                message: e.to_string(),
            })),
        }
    }

    async fn set_digital_output(
        &self,
        request: Request<SetDigitalOutputRequest>,
    ) -> Result<Response<SetDigitalOutputResponse>, Status> {
        let req = request.into_inner();
        info!(
            module = req.module_id,
            channel = req.channel,
            value = req.value,
            "gRPC SetDigitalOutput"
        );

        let cmd = IoCommand {
            module_id: req.module_id,
            channel: req.channel,
            value: req.value,
        };

        let mut mgr = self.manager.lock().await;
        match mgr.set_digital_output(cmd) {
            Ok(()) => Ok(Response::new(SetDigitalOutputResponse {
                success: true,
                message: "Output set".into(),
            })),
            Err(e) => Ok(Response::new(SetDigitalOutputResponse {
                success: false,
                message: e.to_string(),
            })),
        }
    }

    async fn get_digital_input(
        &self,
        request: Request<GetDigitalInputRequest>,
    ) -> Result<Response<GetDigitalInputResponse>, Status> {
        let req = request.into_inner();
        let mgr = self.manager.lock().await;

        match mgr.get_digital_input(req.module_id, req.channel) {
            Ok((value, all)) => Ok(Response::new(GetDigitalInputResponse {
                value,
                all_inputs: all as u32,
            })),
            Err(e) => Err(Status::not_found(e.to_string())),
        }
    }
}
