use std::collections::HashMap;

use anyhow::Result;
use tracing::info;

use crate::device::io_module::IoModule;
use crate::device::motion_axis::MotionAxis;
use crate::device::types::{DeviceKind, IoCommand, MotionFeedback, MotionGoal, MotionResult};
use crate::ethercat::slave::SlaveInfo;

/// Central registry for all devices discovered on the EtherCAT bus.
///
/// The `DeviceManager` owns the axis and IO module instances and routes
/// commands from the gRPC layer to the correct device.
pub struct DeviceManager {
    /// Motion axes keyed by axis_id.
    pub axes: HashMap<u32, MotionAxis>,
    /// IO modules keyed by module_id.
    pub io_modules: HashMap<u32, IoModule>,
}

impl DeviceManager {
    pub fn new() -> Self {
        Self {
            axes: HashMap::new(),
            io_modules: HashMap::new(),
        }
    }

    /// Auto-register devices from the slave scan results.
    ///
    /// The heuristic is simple:
    /// - Slaves whose vendor/product IDs match known servo/stepper drives are
    ///   registered as `MotionAxis`.
    /// - Slaves whose vendor/product IDs match known IO modules are registered
    ///   as `IoModule`.
    /// - Unknown slaves are logged and skipped.
    pub fn register_from_scan(&mut self, slaves: &[SlaveInfo]) {
        for slave in slaves {
            let kind = classify_slave(slave);
            match kind {
                Some(DeviceKind::MotionAxis) => {
                    let id = slave.index as u32;
                    info!(
                        id,
                        name = slave.name,
                        "Registering motion axis"
                    );
                    self.axes.insert(id, MotionAxis::new(id));
                }
                Some(DeviceKind::IoModule) => {
                    let id = slave.index as u32;
                    info!(
                        id,
                        name = slave.name,
                        "Registering IO module"
                    );
                    self.io_modules.insert(id, IoModule::new(id));
                }
                None => {
                    info!(
                        index = slave.index,
                        name = slave.name,
                        vendor = slave.vendor_id,
                        product = slave.product_id,
                        "Unknown slave — skipping"
                    );
                }
            }
        }
    }

    // -----------------------------------------------------------------------
    // Motion axis operations
    // -----------------------------------------------------------------------

    pub fn send_goal(&mut self, goal: MotionGoal) -> Result<()> {
        let axis = self
            .axes
            .get_mut(&goal.axis_id)
            .ok_or_else(|| anyhow::anyhow!("Axis {} not found", goal.axis_id))?;
        axis.send_goal(goal)
    }

    pub fn get_feedback(&self, axis_id: u32) -> Result<MotionFeedback> {
        let axis = self
            .axes
            .get(&axis_id)
            .ok_or_else(|| anyhow::anyhow!("Axis {} not found", axis_id))?;
        // We return feedback with position 0 here; the real position comes
        // from the cyclic tick.  The gRPC server should read the latest
        // feedback snapshot from a shared state instead.  This is a
        // placeholder that returns the state without a live position.
        Ok(axis.get_feedback(0))
    }

    pub fn get_result(&self, axis_id: u32) -> Result<Option<MotionResult>> {
        let axis = self
            .axes
            .get(&axis_id)
            .ok_or_else(|| anyhow::anyhow!("Axis {} not found", axis_id))?;
        Ok(axis.get_result().cloned())
    }

    pub fn halt(&self, axis_id: u32) -> Result<()> {
        let axis = self
            .axes
            .get(&axis_id)
            .ok_or_else(|| anyhow::anyhow!("Axis {} not found", axis_id))?;
        axis.halt();
        Ok(())
    }

    // -----------------------------------------------------------------------
    // IO module operations
    // -----------------------------------------------------------------------

    pub fn set_digital_output(&mut self, cmd: IoCommand) -> Result<()> {
        let module = self
            .io_modules
            .get_mut(&cmd.module_id)
            .ok_or_else(|| anyhow::anyhow!("IO module {} not found", cmd.module_id))?;
        module.set_output(cmd.channel, cmd.value)
    }

    pub fn get_digital_input(&self, module_id: u32, channel: u32) -> Result<(bool, u16)> {
        let module = self
            .io_modules
            .get(&module_id)
            .ok_or_else(|| anyhow::anyhow!("IO module {} not found", module_id))?;

        if channel == 0xFFFF {
            // Return all inputs.
            Ok((false, module.get_all_inputs()))
        } else {
            let value = module.get_input(channel)?;
            Ok((value, module.get_all_inputs()))
        }
    }
}

// ---------------------------------------------------------------------------
// Slave classification heuristic
// ---------------------------------------------------------------------------

/// Known vendor/product ID pairs.
///
/// Inovance SV660NS: vendor 0x00100000, product varies (placeholder).
/// Moons STF05-ECX:  vendor 0x000001A1, product varies (placeholder).
/// Inovance EC3A-IO: vendor 0x00100000, product varies (placeholder).
///
/// In practice these should be read from a configuration file or discovered
/// from the slave's CoE Object Dictionary.  For now we use a simple
/// heuristic based on the slave name string.
fn classify_slave(slave: &SlaveInfo) -> Option<DeviceKind> {
    let name_lower = slave.name.to_lowercase();

    // Inovance servo
    if name_lower.contains("sv660") || name_lower.contains("servo") {
        return Some(DeviceKind::MotionAxis);
    }
    // Moons stepper
    if name_lower.contains("stf05") || name_lower.contains("stepper") || name_lower.contains("moons") {
        return Some(DeviceKind::MotionAxis);
    }
    // Inovance IO module
    if name_lower.contains("ec3a") || name_lower.contains("io16") || name_lower.contains("io module") {
        return Some(DeviceKind::IoModule);
    }

    // Fallback: if the slave supports CiA402 (mode-of-operation objects),
    // treat as a motion axis.  This is a rough heuristic.
    if slave.has_cia402 {
        return Some(DeviceKind::MotionAxis);
    }

    None
}
