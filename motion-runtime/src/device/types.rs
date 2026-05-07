/// Kind of device discovered on the EtherCAT bus.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeviceKind {
    /// Servo or stepper drive (CiA402 PP mode).
    MotionAxis,
    /// Digital IO module.
    IoModule,
}

/// A motion goal sent from the Python BT engine.
#[derive(Debug, Clone)]
pub struct MotionGoal {
    pub axis_id: u32,
    pub target_position: i32,
    pub velocity: u32,
    pub timeout_secs: f32,
}

/// Real-time feedback for a motion axis.
#[derive(Debug, Clone)]
pub struct MotionFeedback {
    pub axis_id: u32,
    pub current_position: i32,
    pub state: String,
    pub progress_pct: f32,
}

/// Final result of a completed (or failed) motion.
#[derive(Debug, Clone)]
pub struct MotionResult {
    pub axis_id: u32,
    pub success: bool,
    pub final_position: i32,
    pub error_code: u32,
    pub error_msg: String,
}

/// Command to set a single digital output.
#[derive(Debug, Clone)]
pub struct IoCommand {
    pub module_id: u32,
    pub channel: u32,
    pub value: bool,
}

/// Snapshot of a digital IO module's state.
#[derive(Debug, Clone)]
pub struct IoState {
    pub module_id: u32,
    pub inputs: u16,
    pub outputs: u16,
}
