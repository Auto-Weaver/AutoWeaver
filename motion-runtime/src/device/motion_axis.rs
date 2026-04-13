use std::sync::atomic::{AtomicBool, Ordering};

use anyhow::{bail, Result};
use tracing::{debug, info, warn};

use crate::cia402::state_machine::Cia402StateMachine;
use crate::cia402::types::{Cia402State, SW_SETPOINT_ACK, SW_TARGET_REACHED};
use crate::device::types::{MotionFeedback, MotionGoal, MotionResult};

// ---------------------------------------------------------------------------
// PDO layout offsets (byte offsets within the slave's PDO image).
// These are typical for CiA402 PP mode and match both the SV660NS and STF05.
// ---------------------------------------------------------------------------

/// Offset of the 16-bit controlword in the output (RxPDO) image.
const CONTROLWORD_OFFSET: usize = 0;
/// Offset of the 32-bit target position in the output image.
const TARGET_POSITION_OFFSET: usize = 2;
/// Offset of the 32-bit profile velocity in the output image.
const PROFILE_VELOCITY_OFFSET: usize = 6;

/// Offset of the 16-bit statusword in the input (TxPDO) image.
const STATUSWORD_OFFSET: usize = 0;
/// Offset of the 32-bit actual position in the input image.
const ACTUAL_POSITION_OFFSET: usize = 2;

/// Represents a single CiA402 motion axis (servo or stepper).
///
/// This struct is designed to be used inside the EtherCAT cyclic loop.  Each
/// cycle the caller reads the slave input PDO, calls [`MotionAxis::tick`], and
/// then writes the output PDO.
pub struct MotionAxis {
    /// Unique axis identifier.
    pub axis_id: u32,
    /// CiA402 state machine tracker.
    sm: Cia402StateMachine,
    /// Active goal, if any.
    active_goal: Option<MotionGoal>,
    /// Latest result (set when a goal finishes or fails).
    last_result: Option<MotionResult>,
    /// Start position when the current goal was accepted.
    start_position: i32,
    /// Halt flag — set by the gRPC halt handler.
    halt_requested: AtomicBool,
    /// PP-mode set-point handshake state.
    setpoint_sent: bool,
}

impl MotionAxis {
    pub fn new(axis_id: u32) -> Self {
        Self {
            axis_id,
            sm: Cia402StateMachine::new(axis_id),
            active_goal: None,
            last_result: None,
            start_position: 0,
            halt_requested: AtomicBool::new(false),
            setpoint_sent: false,
        }
    }

    // -----------------------------------------------------------------------
    // Goal / feedback / result interface (called from gRPC handlers)
    // -----------------------------------------------------------------------

    /// Accept a new motion goal.  Returns an error if the axis is busy.
    pub fn send_goal(&mut self, goal: MotionGoal) -> Result<()> {
        if self.active_goal.is_some() {
            bail!("Axis {} is busy with an active goal", self.axis_id);
        }
        info!(
            axis = self.axis_id,
            target = goal.target_position,
            vel = goal.velocity,
            "Accepting motion goal"
        );
        self.active_goal = Some(goal);
        self.setpoint_sent = false;
        self.last_result = None;
        Ok(())
    }

    /// Return real-time feedback.
    pub fn get_feedback(&self, current_position: i32) -> MotionFeedback {
        let progress = if let Some(ref goal) = self.active_goal {
            let total = (goal.target_position - self.start_position).abs() as f32;
            if total == 0.0 {
                100.0
            } else {
                let done = (current_position - self.start_position).abs() as f32;
                (done / total * 100.0).clamp(0.0, 100.0)
            }
        } else {
            0.0
        };

        MotionFeedback {
            axis_id: self.axis_id,
            current_position,
            state: self.sm.current_state().to_string(),
            progress_pct: progress,
        }
    }

    /// Return the result of the last completed goal (if any).
    pub fn get_result(&self) -> Option<&MotionResult> {
        self.last_result.as_ref()
    }

    /// Request an immediate halt.
    pub fn halt(&self) {
        self.halt_requested.store(true, Ordering::Relaxed);
    }

    // -----------------------------------------------------------------------
    // Cyclic tick — called every EtherCAT cycle
    // -----------------------------------------------------------------------

    /// Process one EtherCAT cycle.
    ///
    /// * `input_pdo`  — the slave's TxPDO (input image) bytes.
    /// * `output_pdo` — the slave's RxPDO (output image) bytes (mutable).
    ///
    /// The caller is responsible for providing correctly-sized slices that
    /// match the slave's PDO mapping.
    pub fn tick(&mut self, input_pdo: &[u8], output_pdo: &mut [u8]) {
        // --- Read statusword and actual position from input PDO ---
        let statusword = read_u16(input_pdo, STATUSWORD_OFFSET);
        let actual_position = read_i32(input_pdo, ACTUAL_POSITION_OFFSET);

        // --- Determine desired controlword ---
        let controlword = if self.halt_requested.load(Ordering::Relaxed) {
            self.handle_halt(actual_position);
            self.sm.halt_controlword()
        } else if self.active_goal.is_some() {
            self.handle_motion(statusword, actual_position)
        } else {
            // No active goal — keep the drive in OperationEnabled (or bring it there).
            self.sm
                .next_controlword(statusword, Cia402State::OperationEnabled)
                .unwrap_or(0)
        };

        // --- Write controlword and target position / velocity to output PDO ---
        write_u16(output_pdo, CONTROLWORD_OFFSET, controlword);

        if let Some(ref goal) = self.active_goal {
            write_i32(output_pdo, TARGET_POSITION_OFFSET, goal.target_position);
            write_u32(output_pdo, PROFILE_VELOCITY_OFFSET, goal.velocity);
        }
    }

    // -----------------------------------------------------------------------
    // Internal helpers
    // -----------------------------------------------------------------------

    fn handle_motion(&mut self, statusword: u16, actual_position: i32) -> u16 {
        // Step 1: bring the drive to OperationEnabled
        let state = self.sm.current_state();
        if state != Cia402State::OperationEnabled {
            return self
                .sm
                .next_controlword(statusword, Cia402State::OperationEnabled)
                .unwrap_or(0);
        }

        // We must call update to keep the state machine in sync.
        self.sm.update(statusword);

        // Step 2: PP-mode set-point handshake
        if !self.setpoint_sent {
            // Record the start position for progress calculation.
            self.start_position = actual_position;
            self.setpoint_sent = true;
            debug!(axis = self.axis_id, "Sending PP set-point");
            return self.sm.pp_start_controlword();
        }

        // Step 3: Check if the drive acknowledged the set-point
        let ack = statusword & SW_SETPOINT_ACK != 0;
        let reached = statusword & SW_TARGET_REACHED != 0;

        if ack && !reached {
            // Set-point acknowledged, clear the new-set-point bit and wait.
            return self.sm.pp_hold_controlword();
        }

        if reached {
            // Motion complete.
            info!(
                axis = self.axis_id,
                pos = actual_position,
                "Target reached"
            );
            self.complete_goal(true, actual_position, 0, String::new());
            return self.sm.pp_hold_controlword();
        }

        // Still moving — hold.
        self.sm.pp_hold_controlword()
    }

    fn handle_halt(&mut self, actual_position: i32) {
        self.halt_requested.store(false, Ordering::Relaxed);
        warn!(axis = self.axis_id, "Halt executed");
        self.complete_goal(false, actual_position, 1, "Halted by user".into());
    }

    fn complete_goal(
        &mut self,
        success: bool,
        final_position: i32,
        error_code: u32,
        error_msg: String,
    ) {
        self.last_result = Some(MotionResult {
            axis_id: self.axis_id,
            success,
            final_position,
            error_code,
            error_msg,
        });
        self.active_goal = None;
        self.setpoint_sent = false;
    }
}

// ---------------------------------------------------------------------------
// PDO byte helpers (little-endian)
// ---------------------------------------------------------------------------

fn read_u16(buf: &[u8], offset: usize) -> u16 {
    if buf.len() < offset + 2 {
        return 0;
    }
    u16::from_le_bytes([buf[offset], buf[offset + 1]])
}

fn read_i32(buf: &[u8], offset: usize) -> i32 {
    if buf.len() < offset + 4 {
        return 0;
    }
    i32::from_le_bytes([
        buf[offset],
        buf[offset + 1],
        buf[offset + 2],
        buf[offset + 3],
    ])
}

fn write_u16(buf: &mut [u8], offset: usize, value: u16) {
    if buf.len() >= offset + 2 {
        let bytes = value.to_le_bytes();
        buf[offset] = bytes[0];
        buf[offset + 1] = bytes[1];
    }
}

fn write_i32(buf: &mut [u8], offset: usize, value: i32) {
    if buf.len() >= offset + 4 {
        let bytes = value.to_le_bytes();
        buf[offset..offset + 4].copy_from_slice(&bytes);
    }
}

fn write_u32(buf: &mut [u8], offset: usize, value: u32) {
    if buf.len() >= offset + 4 {
        let bytes = value.to_le_bytes();
        buf[offset..offset + 4].copy_from_slice(&bytes);
    }
}
