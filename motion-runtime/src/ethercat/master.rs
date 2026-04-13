use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use ethercrab::{
    std::{ethercat_now, tx_rx_task},
    MainDevice, MainDeviceConfig, PduStorage, Timeouts,
};
use tokio::sync::Mutex;
use tokio::time::MissedTickBehavior;
use tracing::info;

use crate::device::manager::DeviceManager;
use crate::device::types::DeviceKind;
use crate::ethercat::slave::SlaveInfo;

/// Number of SubDevices we can support (must be a power of 2 > 1).
const MAX_SUBDEVICES: usize = 16;
/// Maximum PDI (Process Data Image) length in bytes.
const MAX_PDI_LEN: usize = 256;
/// Maximum PDU data bytes per frame.
const MAX_PDU_DATA: usize = PduStorage::element_size(1100);
/// Number of storage elements (frames).
const MAX_FRAMES: usize = 32;
/// Cycle time in microseconds (1 ms).
const CYCLE_US: u64 = 1000;

/// Static PDU storage — must live for the entire program lifetime.
static PDU_STORAGE: PduStorage<MAX_FRAMES, MAX_PDU_DATA> = PduStorage::new();

/// Initialize the EtherCAT master, scan slaves, register devices, and run
/// the cyclic process-data loop.
///
/// This is the main entry point for the EtherCAT subsystem.  It runs forever
/// (or until an error occurs).
pub async fn run(
    interface: &str,
    manager: Arc<Mutex<DeviceManager>>,
) -> Result<()> {
    info!(interface, "Initializing EtherCAT master");

    let (tx, rx, pdu_loop) = PDU_STORAGE
        .try_split()
        .map_err(|_| anyhow::anyhow!("PDU storage already split — run() may only be called once"))?;

    let main_device = MainDevice::new(pdu_loop, Timeouts::default(), MainDeviceConfig::default());

    // Spawn the low-level TX/RX task that talks to the NIC.
    tokio::spawn(tx_rx_task(interface, tx, rx).expect("Failed to open raw socket"));

    info!("Scanning EtherCAT bus...");

    let group = main_device
        .init_single_group::<MAX_SUBDEVICES, MAX_PDI_LEN>(ethercat_now)
        .await
        .context("Failed to initialise SubDevice group")?;

    // Collect slave info before transitioning to OP.
    let mut slaves = Vec::new();
    for subdevice in group.iter(&main_device) {
        let identity = subdevice.identity();

        slaves.push(SlaveInfo {
            index: subdevice.configured_address(),
            name: subdevice.name().to_string(),
            vendor_id: identity.vendor_id,
            product_id: identity.product_id,
            has_cia402: false,
            input_pdo_size: 0,
            output_pdo_size: 0,
        });
    }

    info!(count = slaves.len(), "Slave scan complete");

    // Register devices in the manager.
    {
        let mut mgr = manager.lock().await;
        mgr.register_from_scan(&slaves);
    }

    // Build index: (slave_address -> DeviceKind) for fast dispatch.
    let slave_dispatch: Vec<(u16, DeviceKind)> = {
        let mgr = manager.lock().await;
        slaves
            .iter()
            .filter_map(|s| {
                let id = s.index as u32;
                if mgr.axes.contains_key(&id) {
                    Some((s.index, DeviceKind::MotionAxis))
                } else if mgr.io_modules.contains_key(&id) {
                    Some((s.index, DeviceKind::IoModule))
                } else {
                    None
                }
            })
            .collect()
    };

    // Transition from PRE-OP -> SAFE-OP -> OP.
    let group = group
        .into_op(&main_device)
        .await
        .context("Failed to transition group to OP")?;

    for subdevice in group.iter(&main_device) {
        info!(
            addr = subdevice.configured_address(),
            name = subdevice.name(),
            "SubDevice in OP"
        );
    }

    // --- Cyclic process-data loop ---
    info!("Starting cyclic loop ({}us)", CYCLE_US);

    let mut interval = tokio::time::interval(Duration::from_micros(CYCLE_US));
    interval.set_missed_tick_behavior(MissedTickBehavior::Skip);

    loop {
        interval.tick().await;

        // Exchange process data with all slaves.
        group
            .tx_rx(&main_device)
            .await
            .context("Cyclic TX/RX failed")?;

        // Use try_lock to avoid blocking the real-time loop.  If the gRPC
        // handler holds the lock we skip one cycle (acceptable latency).
        let Ok(mut mgr) = manager.try_lock() else {
            continue;
        };

        // Iterate all subdevices and dispatch to the matching handler.
        // We read inputs into a local buffer first, then get mutable
        // access to outputs.  The PDO data is small (< 64 bytes), so
        // the stack copy is negligible.
        for subdevice in group.iter(&main_device) {
            let addr = subdevice.configured_address();
            let kind = slave_dispatch
                .iter()
                .find(|(a, _)| *a == addr)
                .map(|(_, k)| *k);

            if kind.is_none() {
                continue;
            }

            // Snapshot inputs into a stack buffer first, then get
            // mutable access to outputs. This avoids borrow conflicts.
            let mut input_buf = [0u8; 64];
            let input_len;
            {
                let io = subdevice.io_raw();
                let inp = io.inputs();
                input_len = inp.len().min(input_buf.len());
                input_buf[..input_len].copy_from_slice(&inp[..input_len]);
            }

            {
                let mut io = subdevice.io_raw_mut();
                let outputs = io.outputs();

                match kind.unwrap() {
                    DeviceKind::MotionAxis => {
                        let id = addr as u32;
                        if let Some(axis) = mgr.axes.get_mut(&id) {
                            axis.tick(&input_buf[..input_len], outputs);
                        }
                    }
                    DeviceKind::IoModule => {
                        let id = addr as u32;
                        if let Some(io_mod) = mgr.io_modules.get_mut(&id) {
                            io_mod.tick(&input_buf[..input_len], outputs);
                        }
                    }
                }
            }
        }
    }
}
