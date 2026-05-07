use tracing::{debug, warn};

use super::types::{
    controlword, decode_state, Cia402State, CW_CHANGE_SET_IMMEDIATELY, CW_HALT, CW_NEW_SETPOINT,
};

/// Tracks the CiA402 state machine for a single axis and computes the
/// controlword required to reach a target state.
#[derive(Debug)]
pub struct Cia402StateMachine {
    /// Axis identifier (for logging).
    axis_id: u32,
    /// Last observed CiA402 state (decoded from statusword).
    current_state: Cia402State,
    /// Whether a fault-reset rising edge has already been sent.
    fault_reset_sent: bool,
}

impl Cia402StateMachine {
    pub fn new(axis_id: u32) -> Self {
        Self {
            axis_id,
            current_state: Cia402State::Unknown,
            fault_reset_sent: false,
        }
    }

    /// Return the last decoded state.
    pub fn current_state(&self) -> Cia402State {
        self.current_state
    }

    /// Feed in a new statusword, decode it, and return the decoded state.
    pub fn update(&mut self, statusword: u16) -> Cia402State {
        let state = decode_state(statusword);
        if state != self.current_state {
            debug!(
                axis = self.axis_id,
                from = %self.current_state,
                to = %state,
                "CiA402 state transition"
            );
        }
        // Reset fault-reset flag when we leave the Fault state
        if self.current_state == Cia402State::Fault && state != Cia402State::Fault {
            self.fault_reset_sent = false;
        }
        self.current_state = state;
        state
    }

    /// Compute the controlword needed to move from the current state toward
    /// `target`.  Returns `None` if we are already in the target state.
    ///
    /// This performs a single step; the caller should invoke this each cycle
    /// until the target is reached.
    pub fn next_controlword(&mut self, statusword: u16, target: Cia402State) -> Option<u16> {
        let current = self.update(statusword);

        if current == target {
            // Already there — return the "hold" controlword for this state.
            return Some(hold_controlword(current));
        }

        // Handle fault first — regardless of target.
        if current == Cia402State::Fault {
            return Some(self.handle_fault());
        }

        Some(transition_controlword(current, target, self.axis_id))
    }

    /// Build a controlword that starts a PP-mode motion (new set-point + change
    /// set immediately) on top of the OperationEnabled base word.
    pub fn pp_start_controlword(&self) -> u16 {
        controlword::ENABLE_OPERATION | CW_NEW_SETPOINT | CW_CHANGE_SET_IMMEDIATELY
    }

    /// Build a controlword that clears the new-set-point bit (needed after the
    /// drive has acknowledged the set-point).
    pub fn pp_hold_controlword(&self) -> u16 {
        controlword::ENABLE_OPERATION
    }

    /// Build a halt controlword.
    pub fn halt_controlword(&self) -> u16 {
        controlword::ENABLE_OPERATION | CW_HALT
    }

    // -----------------------------------------------------------------------
    // Internal helpers
    // -----------------------------------------------------------------------

    fn handle_fault(&mut self) -> u16 {
        if !self.fault_reset_sent {
            warn!(axis = self.axis_id, "Sending fault reset");
            self.fault_reset_sent = true;
            controlword::FAULT_RESET
        } else {
            // Keep the rising edge high for one more cycle, then the update()
            // call next cycle will detect the state change.
            controlword::FAULT_RESET
        }
    }
}

/// Choose the controlword to transition from `current` toward `target`.
fn transition_controlword(current: Cia402State, target: Cia402State, axis_id: u32) -> u16 {
    use Cia402State::*;

    match (current, target) {
        // ------ moving UP toward OperationEnabled ------
        (SwitchOnDisabled, ReadyToSwitchOn | SwitchedOn | OperationEnabled) => {
            controlword::SHUTDOWN
        }
        (NotReadyToSwitchOn, _) => {
            // Drive is booting; we can only wait.  Send disable-voltage (safe).
            controlword::DISABLE_VOLTAGE
        }
        (ReadyToSwitchOn, SwitchedOn | OperationEnabled) => controlword::SWITCH_ON,
        (ReadyToSwitchOn, ReadyToSwitchOn) => controlword::SHUTDOWN,
        (SwitchedOn, OperationEnabled) => controlword::ENABLE_OPERATION,
        (SwitchedOn, SwitchedOn) => controlword::SWITCH_ON,

        // ------ moving DOWN ------
        (OperationEnabled, SwitchedOn) => controlword::SWITCH_ON, // disable operation
        (OperationEnabled, ReadyToSwitchOn) => controlword::SHUTDOWN,
        (OperationEnabled, SwitchOnDisabled) => controlword::DISABLE_VOLTAGE,

        (QuickStopActive, SwitchOnDisabled) => controlword::DISABLE_VOLTAGE,
        (QuickStopActive, _) => controlword::DISABLE_VOLTAGE,

        (FaultReactionActive, _) => {
            // Nothing we can do; wait for the drive to transition to Fault.
            0x0000
        }

        _ => {
            warn!(
                axis = axis_id,
                from = %current,
                to = %target,
                "No direct CiA402 transition; sending disable-voltage"
            );
            controlword::DISABLE_VOLTAGE
        }
    }
}

/// Return the "hold" controlword that keeps the drive in the given state.
fn hold_controlword(state: Cia402State) -> u16 {
    match state {
        Cia402State::OperationEnabled => controlword::ENABLE_OPERATION,
        Cia402State::SwitchedOn => controlword::SWITCH_ON,
        Cia402State::ReadyToSwitchOn => controlword::SHUTDOWN,
        Cia402State::SwitchOnDisabled => controlword::DISABLE_VOLTAGE,
        _ => 0x0000,
    }
}
