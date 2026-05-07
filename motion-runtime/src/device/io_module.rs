use anyhow::{bail, Result};
use tracing::debug;

use crate::device::types::IoState;

/// Byte offset of the 16-bit digital outputs in the RxPDO (output image).
const DO_OFFSET: usize = 0;
/// Byte offset of the 16-bit digital inputs in the TxPDO (input image).
const DI_OFFSET: usize = 0;

/// Maximum number of digital channels per bank.
const MAX_CHANNELS: u32 = 16;

/// Represents an EtherCAT digital IO module (e.g. Inovance EC3A-IO1632).
///
/// 16 digital inputs (PNP) + 16 digital outputs (PNP).
pub struct IoModule {
    /// Unique module identifier.
    pub module_id: u32,
    /// Shadow copy of the output bits (written to PDO each cycle).
    outputs: u16,
    /// Latest snapshot of the input bits (read from PDO each cycle).
    inputs: u16,
}

impl IoModule {
    pub fn new(module_id: u32) -> Self {
        Self {
            module_id,
            outputs: 0,
            inputs: 0,
        }
    }

    // -----------------------------------------------------------------------
    // Public interface (called from gRPC handlers)
    // -----------------------------------------------------------------------

    /// Set a single digital output channel (0-15).
    pub fn set_output(&mut self, channel: u32, value: bool) -> Result<()> {
        if channel >= MAX_CHANNELS {
            bail!(
                "Channel {} out of range for IO module {} (max {})",
                channel,
                self.module_id,
                MAX_CHANNELS - 1
            );
        }
        if value {
            self.outputs |= 1 << channel;
        } else {
            self.outputs &= !(1 << channel);
        }
        debug!(
            module = self.module_id,
            channel,
            value,
            outputs = self.outputs,
            "Set digital output"
        );
        Ok(())
    }

    /// Read a single digital input channel (0-15).
    pub fn get_input(&self, channel: u32) -> Result<bool> {
        if channel >= MAX_CHANNELS {
            bail!(
                "Channel {} out of range for IO module {} (max {})",
                channel,
                self.module_id,
                MAX_CHANNELS - 1
            );
        }
        Ok(self.inputs & (1 << channel) != 0)
    }

    /// Read all 16 digital inputs as a bitmask.
    pub fn get_all_inputs(&self) -> u16 {
        self.inputs
    }

    /// Read all 16 digital outputs as a bitmask.
    pub fn get_all_outputs(&self) -> u16 {
        self.outputs
    }

    /// Get a full state snapshot.
    pub fn get_state(&self) -> IoState {
        IoState {
            module_id: self.module_id,
            inputs: self.inputs,
            outputs: self.outputs,
        }
    }

    // -----------------------------------------------------------------------
    // Cyclic tick — called every EtherCAT cycle
    // -----------------------------------------------------------------------

    /// Process one EtherCAT cycle.
    ///
    /// * `input_pdo`  — the slave's TxPDO (input image) bytes.
    /// * `output_pdo` — the slave's RxPDO (output image) bytes (mutable).
    pub fn tick(&mut self, input_pdo: &[u8], output_pdo: &mut [u8]) {
        // Read digital inputs from the input PDO.
        self.inputs = read_u16(input_pdo, DI_OFFSET);

        // Write digital outputs to the output PDO.
        write_u16(output_pdo, DO_OFFSET, self.outputs);
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

fn write_u16(buf: &mut [u8], offset: usize, value: u16) {
    if buf.len() >= offset + 2 {
        let bytes = value.to_le_bytes();
        buf[offset] = bytes[0];
        buf[offset + 1] = bytes[1];
    }
}
