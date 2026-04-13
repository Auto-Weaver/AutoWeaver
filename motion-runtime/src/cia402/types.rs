/// CiA402 drive state machine states.
///
/// Decoded from the statusword according to IEC 61800-7-204 / CiA 402.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cia402State {
    NotReadyToSwitchOn,
    SwitchOnDisabled,
    ReadyToSwitchOn,
    SwitchedOn,
    OperationEnabled,
    QuickStopActive,
    FaultReactionActive,
    Fault,
    Unknown,
}

impl std::fmt::Display for Cia402State {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let s = match self {
            Self::NotReadyToSwitchOn => "NotReadyToSwitchOn",
            Self::SwitchOnDisabled => "SwitchOnDisabled",
            Self::ReadyToSwitchOn => "ReadyToSwitchOn",
            Self::SwitchedOn => "SwitchedOn",
            Self::OperationEnabled => "OperationEnabled",
            Self::QuickStopActive => "QuickStopActive",
            Self::FaultReactionActive => "FaultReactionActive",
            Self::Fault => "Fault",
            Self::Unknown => "Unknown",
        };
        write!(f, "{}", s)
    }
}

// ---------------------------------------------------------------------------
// Statusword bit masks for state decoding
// ---------------------------------------------------------------------------

/// Bits 0..3 + bit 5 + bit 6 are used to determine the CiA402 state.
const SW_STATE_MASK: u16 = 0b0110_1111; // bits 0-3, 5, 6

/// Bit 10: Target reached
pub const SW_TARGET_REACHED: u16 = 1 << 10;

/// Bit 12: Set-point acknowledge (PP mode)
pub const SW_SETPOINT_ACK: u16 = 1 << 12;

/// Bit 13: Following error (some drives)
pub const SW_FOLLOWING_ERROR: u16 = 1 << 13;

/// Bit 3: Fault
pub const SW_FAULT: u16 = 1 << 3;

/// Bit 7: Warning
pub const SW_WARNING: u16 = 1 << 7;

/// Decode the statusword into a `Cia402State`.
pub fn decode_state(statusword: u16) -> Cia402State {
    let masked = statusword & SW_STATE_MASK;

    match masked {
        // Not ready to switch on: xxxx xxxx x0xx 0000
        v if v & 0b0100_1111 == 0b0000_0000 => Cia402State::NotReadyToSwitchOn,
        // Switch on disabled: xxxx xxxx x1xx 0000
        v if v & 0b0100_1111 == 0b0100_0000 => Cia402State::SwitchOnDisabled,
        // Ready to switch on: xxxx xxxx x01x 0001
        v if v & 0b0110_1111 == 0b0010_0001 => Cia402State::ReadyToSwitchOn,
        // Switched on: xxxx xxxx x01x 0011
        v if v & 0b0110_1111 == 0b0010_0011 => Cia402State::SwitchedOn,
        // Operation enabled: xxxx xxxx x01x 0111
        v if v & 0b0110_1111 == 0b0010_0111 => Cia402State::OperationEnabled,
        // Quick stop active: xxxx xxxx x00x 0111
        v if v & 0b0110_1111 == 0b0000_0111 => Cia402State::QuickStopActive,
        // Fault reaction active: xxxx xxxx x0xx 1111
        v if v & 0b0100_1111 == 0b0000_1111 => Cia402State::FaultReactionActive,
        // Fault: xxxx xxxx x0xx 1000
        v if v & 0b0100_1111 == 0b0000_1000 => Cia402State::Fault,
        _ => Cia402State::Unknown,
    }
}

// ---------------------------------------------------------------------------
// Controlword bit definitions
// ---------------------------------------------------------------------------

/// Bit 0: Switch on
pub const CW_SWITCH_ON: u16 = 1 << 0;
/// Bit 1: Enable voltage
pub const CW_ENABLE_VOLTAGE: u16 = 1 << 1;
/// Bit 2: Quick stop (active low in CiA402 — 0 = quick stop, 1 = normal)
pub const CW_QUICK_STOP: u16 = 1 << 2;
/// Bit 3: Enable operation
pub const CW_ENABLE_OPERATION: u16 = 1 << 3;
/// Bit 4: New set-point (PP mode)
pub const CW_NEW_SETPOINT: u16 = 1 << 4;
/// Bit 5: Change set immediately (PP mode)
pub const CW_CHANGE_SET_IMMEDIATELY: u16 = 1 << 5;
/// Bit 7: Fault reset (rising edge)
pub const CW_FAULT_RESET: u16 = 1 << 7;
/// Bit 8: Halt
pub const CW_HALT: u16 = 1 << 8;

/// Controlword presets for CiA402 state transitions.
pub mod controlword {
    use super::*;

    /// Shutdown: transition to ReadyToSwitchOn
    pub const SHUTDOWN: u16 = CW_ENABLE_VOLTAGE | CW_QUICK_STOP; // 0x0006

    /// Switch on: transition to SwitchedOn
    pub const SWITCH_ON: u16 = CW_SWITCH_ON | CW_ENABLE_VOLTAGE | CW_QUICK_STOP; // 0x0007

    /// Enable operation: transition to OperationEnabled
    pub const ENABLE_OPERATION: u16 =
        CW_SWITCH_ON | CW_ENABLE_VOLTAGE | CW_QUICK_STOP | CW_ENABLE_OPERATION; // 0x000F

    /// Disable voltage: transition to SwitchOnDisabled
    pub const DISABLE_VOLTAGE: u16 = 0x0000;

    /// Quick stop
    pub const QUICK_STOP: u16 = CW_ENABLE_VOLTAGE; // 0x0002

    /// Fault reset (rising edge on bit 7)
    pub const FAULT_RESET: u16 = CW_FAULT_RESET; // 0x0080
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_decode_switch_on_disabled() {
        // statusword = 0x0250 => masked = 0x0040 => SwitchOnDisabled
        assert_eq!(decode_state(0x0250), Cia402State::SwitchOnDisabled);
    }

    #[test]
    fn test_decode_operation_enabled() {
        // statusword = 0x1237 => masked = 0x0027 => OperationEnabled
        assert_eq!(decode_state(0x1237), Cia402State::OperationEnabled);
    }

    #[test]
    fn test_decode_fault() {
        // statusword = 0x0008 => Fault
        assert_eq!(decode_state(0x0008), Cia402State::Fault);
    }
}
