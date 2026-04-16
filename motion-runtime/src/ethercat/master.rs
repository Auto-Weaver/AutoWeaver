use std::sync::Arc;
use std::time::Duration;

use anyhow::{bail, Context, Result};
use tokio::sync::Mutex;
use tokio::time::MissedTickBehavior;
use tracing::{debug, info, warn};

use crate::device::manager::DeviceManager;
use crate::device::types::DeviceKind;
use crate::ethercat::igh_ffi::*;
use crate::ethercat::slave::SlaveInfo;

/// Cycle time in microseconds (1 ms).
const CYCLE_US: u64 = 1000;
/// Cycle time in nanoseconds.
const CYCLE_NS: u32 = (CYCLE_US * 1000) as u32;

/// IgH EtherCAT master index (0 = first master, i.e. /dev/EtherCAT0).
const MASTER_INDEX: u32 = 0;

/// SV660N identity.
const SV660N_VENDOR_ID: u32 = 0x0010_0000;
const SV660N_PRODUCT_CODE: u32 = 0x000c_010d;

/// DC AssignActivate for SYNC0 only.
const DC_ASSIGN_ACTIVATE_SYNC0: u16 = 0x0300;

// ---------------------------------------------------------------------------
// SV660N PDO layout — matches the default mapping from `ethercat pdos`
// ---------------------------------------------------------------------------

// RxPDO 0x1600 entries (output: master → slave)
// The order here must match the slave's default RxPDO mapping.
#[rustfmt::skip]
const SV660N_RXPDO_ENTRIES: &[(u16, u8, u8)] = &[
    // (index, subindex, bit_length)
    (0x60FF, 0x00, 32), // target velocity
    (0x607F, 0x00, 32), // max profile velocity
    (0x6084, 0x00, 32), // profile deceleration
    (0x6083, 0x00, 32), // profile acceleration
    (0x6081, 0x00, 32), // profile velocity
    (0x6087, 0x00, 32), // torque slope
    (0x6071, 0x00, 16), // target torque
    (0x6060, 0x00,  8), // modes of operation
    (0x6040, 0x00, 16), // controlword
    (0x607A, 0x00, 32), // target position
];

// TxPDO 0x1a00 entries (input: slave → master)
#[rustfmt::skip]
const SV660N_TXPDO_ENTRIES: &[(u16, u8, u8)] = &[
    (0x606C, 0x00, 32), // actual velocity
    (0x6077, 0x00, 16), // actual torque
    (0x6061, 0x00,  8), // modes of operation display
    (0x6041, 0x00, 16), // statusword
    (0x6064, 0x00, 32), // actual position
    (0x60B9, 0x00, 16), // touch probe status
    (0x60BA, 0x00, 32), // touch probe pos1 positive
    (0x60BC, 0x00, 32), // touch probe pos2 positive
    (0x603F, 0x00, 16), // error code
    (0x60FD, 0x00, 32), // digital inputs
];

// Byte offsets of the CiA402 objects we care about within the PDO image.
// Computed from the entry order and bit lengths above.

// RxPDO byte offsets (output image)
const OFF_RX_TARGET_VELOCITY: usize = 0;   // 60FF, 4 bytes
const OFF_RX_MAX_PROFILE_VEL: usize = 4;   // 607F, 4 bytes
const OFF_RX_PROFILE_DECEL: usize = 8;     // 6084, 4 bytes
const OFF_RX_PROFILE_ACCEL: usize = 12;    // 6083, 4 bytes
const OFF_RX_PROFILE_VELOCITY: usize = 16; // 6081, 4 bytes
const OFF_RX_TORQUE_SLOPE: usize = 20;     // 6087, 4 bytes
const OFF_RX_TARGET_TORQUE: usize = 24;    // 6071, 2 bytes
const OFF_RX_MODES_OF_OP: usize = 26;      // 6060, 1 byte
const OFF_RX_CONTROLWORD: usize = 27;      // 6040, 2 bytes
const OFF_RX_TARGET_POSITION: usize = 29;  // 607A, 4 bytes
const RXPDO_SIZE: usize = 33;

// TxPDO byte offsets (input image)
const OFF_TX_ACTUAL_VELOCITY: usize = 0;   // 606C, 4 bytes
const OFF_TX_ACTUAL_TORQUE: usize = 4;     // 6077, 2 bytes
const OFF_TX_MODES_DISPLAY: usize = 6;     // 6061, 1 byte
const OFF_TX_STATUSWORD: usize = 7;        // 6041, 2 bytes
const OFF_TX_ACTUAL_POSITION: usize = 9;   // 6064, 4 bytes
const TXPDO_SIZE: usize = 29;

/// Per-slave runtime data: domain buffer offsets and device classification.
struct SlaveRuntime {
    /// Byte offset of this slave's RxPDO (outputs) in the domain buffer.
    output_offset: usize,
    /// Byte offset of this slave's TxPDO (inputs) in the domain buffer.
    input_offset: usize,
    /// Device classification.
    kind: DeviceKind,
    /// Slave position on the bus.
    position: u16,
}

/// Initialize the IgH EtherCAT master, configure slaves, and run the cyclic
/// process-data loop.
///
/// The `interface` parameter is ignored — IgH uses the NIC configured in
/// `/etc/sysconfig/ethercat`.  We keep the parameter for API compatibility.
pub async fn run(
    _interface: &str,
    manager: Arc<Mutex<DeviceManager>>,
) -> Result<()> {
    info!("Initializing IgH EtherCAT master (index {})", MASTER_INDEX);

    // --- Request master ---
    let master = unsafe { ecrt_request_master(MASTER_INDEX) };
    if master.is_null() {
        bail!(
            "ecrt_request_master({}) failed — is the ethercat service running?",
            MASTER_INDEX
        );
    }
    info!("Master acquired");

    // --- Query slave count ---
    let mut master_info: ec_master_info_t = unsafe { std::mem::zeroed() };
    let ret = unsafe { ecrt_master(master, &mut master_info) };
    if ret != 0 {
        bail!("ecrt_master() failed: {}", ret);
    }
    let slave_count = master_info.slave_count;
    info!(slave_count, "Bus scan complete");

    if slave_count == 0 {
        bail!("No slaves found on the bus");
    }

    // --- Collect slave info ---
    let mut slaves = Vec::new();
    for pos in 0..slave_count as u16 {
        let mut si: ec_slave_info_t = unsafe { std::mem::zeroed() };
        let ret = unsafe { ecrt_master_get_slave(master, pos, &mut si) };
        if ret != 0 {
            warn!(position = pos, "Failed to get slave info");
            continue;
        }
        let name = {
            let len = si.name.iter().position(|&b| b == 0).unwrap_or(si.name.len());
            String::from_utf8_lossy(&si.name[..len]).to_string()
        };
        slaves.push(SlaveInfo {
            index: pos,
            name,
            vendor_id: si.vendor_id,
            product_id: si.product_code,
            has_cia402: false,
            input_pdo_size: 0,
            output_pdo_size: 0,
        });
    }

    // --- Register devices in DeviceManager ---
    {
        let mut mgr = manager.lock().await;
        mgr.register_from_scan(&slaves);
    }

    // --- Create domain ---
    let domain = unsafe { ecrt_master_create_domain(master) };
    if domain.is_null() {
        bail!("ecrt_master_create_domain() failed");
    }

    // --- Configure each slave ---
    let mut slave_runtimes: Vec<SlaveRuntime> = Vec::new();

    for slave in &slaves {
        let kind = classify_slave_for_igh(slave);
        if kind.is_none() {
            info!(
                position = slave.index,
                name = slave.name,
                "Unknown slave — skipping configuration"
            );
            continue;
        }
        let kind = kind.unwrap();

        // Get slave config handle
        let sc = unsafe {
            ecrt_master_slave_config(
                master,
                0, // alias
                slave.index,
                slave.vendor_id,
                slave.product_id,
            )
        };
        if sc.is_null() {
            warn!(
                position = slave.index,
                "ecrt_master_slave_config() failed"
            );
            continue;
        }

        match kind {
            DeviceKind::MotionAxis => {
                configure_sv660n(sc, domain, slave, &mut slave_runtimes)?;
            }
            DeviceKind::IoModule => {
                // TODO: configure IO module PDOs when we have one on the bus
                info!(
                    position = slave.index,
                    "IO module configuration not yet implemented"
                );
            }
        }
    }

    // --- Set send interval ---
    unsafe {
        ecrt_master_set_send_interval(master, CYCLE_US as usize);
    }

    // --- Activate master ---
    let ret = unsafe { ecrt_master_activate(master) };
    if ret != 0 {
        bail!("ecrt_master_activate() failed: {}", ret);
    }
    info!("Master activated");

    // --- Get domain data pointer ---
    let domain_data = unsafe { ecrt_domain_data(domain) };
    if domain_data.is_null() {
        bail!("ecrt_domain_data() returned null");
    }
    let domain_size = unsafe { ecrt_domain_size(domain) };
    info!(domain_size, "Domain data mapped");

    let app_time_zero = std::time::Instant::now();

    // --- Cyclic process-data loop ---
    // Keep the full cyclic pump running during startup so the slave sees
    // application time, DC sync, and queued process data before OP is requested.
    info!("Waiting for slave configuration (CoE PDO mapping)...");
    let config_deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    let mut pump_interval = tokio::time::interval(Duration::from_millis(1));
    pump_interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    loop {
        pump_interval.tick().await;
        unsafe {
            ecrt_master_receive(master);
            ecrt_domain_process(domain);
            let app_time = app_time_zero.elapsed().as_nanos() as u64;
            ecrt_master_application_time(master, app_time);
            ecrt_master_sync_reference_clock(master);
            ecrt_master_sync_slave_clocks(master);
            ecrt_domain_queue(domain);
            ecrt_master_send(master);
        }
        // Check if slaves reached at least SAFEOP
        let mut ms: ec_master_state_t = unsafe { std::mem::zeroed() };
        unsafe { ecrt_master_state(master, &mut ms) };
        if ms.al_states() & 0x04 != 0 {
            info!(al_states = ms.al_states(), "Slaves reached SAFEOP+");
            break;
        }
        if tokio::time::Instant::now() > config_deadline {
            tracing::warn!(
                al_states = ms.al_states(),
                "Timeout waiting for SAFEOP — continuing anyway"
            );
            break;
        }
    }

    info!("Starting cyclic loop ({}µs period)", CYCLE_US);

    let mut interval = tokio::time::interval(Duration::from_micros(CYCLE_US));
    interval.set_missed_tick_behavior(MissedTickBehavior::Skip);

    let mut cycle_count: u64 = 0;
    let mut op_confirmed = false;

    loop {
        interval.tick().await;
        cycle_count += 1;

        // 1. Receive datagrams
        unsafe { ecrt_master_receive(master) };

        // 2. Process domain — copies received data into domain buffer
        unsafe { ecrt_domain_process(domain) };

        // 3. Distributed clocks: update application time and sync
        let app_time = app_time_zero.elapsed().as_nanos() as u64;
        unsafe {
            ecrt_master_application_time(master, app_time);
            ecrt_master_sync_reference_clock(master);
            ecrt_master_sync_slave_clocks(master);
        }

        // 4. Check master state periodically
        if !op_confirmed && cycle_count % 2000 == 0 {
            let mut ms: ec_master_state_t = unsafe { std::mem::zeroed() };
            unsafe { ecrt_master_state(master, &mut ms) };
            info!(
                slaves_responding = ms.slaves_responding,
                al_states = ms.al_states(),
                link_up = ms.link_up(),
                "Master state"
            );

            // AL states bit 3 = OP
            if ms.al_states() & 0x08 != 0 {
                op_confirmed = true;
                info!("All slaves in OP");
            }
        }

        // 5. Dispatch to device tick handlers
        if let Ok(mut mgr) = manager.try_lock() {
            for sr in &slave_runtimes {
                // Build input/output slices from the domain buffer
                let input_slice = unsafe {
                    std::slice::from_raw_parts(
                        domain_data.add(sr.input_offset),
                        TXPDO_SIZE,
                    )
                };
                let output_slice = unsafe {
                    std::slice::from_raw_parts_mut(
                        domain_data.add(sr.output_offset),
                        RXPDO_SIZE,
                    )
                };

                match sr.kind {
                    DeviceKind::MotionAxis => {
                        let id = sr.position as u32;
                        if let Some(axis) = mgr.axes.get_mut(&id) {
                            // Remap: the axis tick expects controlword at offset 0,
                            // target_position at offset 2, profile_velocity at offset 6
                            // in the output buffer.  But SV660N's RxPDO has a different
                            // layout.  We use a translation layer.
                            tick_axis_sv660n(axis, input_slice, output_slice);
                        }
                    }
                    DeviceKind::IoModule => {
                        let id = sr.position as u32;
                        if let Some(io_mod) = mgr.io_modules.get_mut(&id) {
                            io_mod.tick(input_slice, output_slice);
                        }
                    }
                }
            }
        }

        // 6. Queue domain for sending
        unsafe { ecrt_domain_queue(domain) };

        // 7. Send datagrams
        unsafe { ecrt_master_send(master) };
    }
}

// ---------------------------------------------------------------------------
// SV660N slave configuration
// ---------------------------------------------------------------------------

fn configure_sv660n(
    sc: *mut ec_slave_config_t,
    domain: *mut ec_domain_t,
    slave: &SlaveInfo,
    runtimes: &mut Vec<SlaveRuntime>,
) -> Result<()> {
    info!(
        position = slave.index,
        name = slave.name,
        "Configuring SV660N servo"
    );

    // Configure PDO mapping via ecrt_slave_config_pdos.
    // SM0/SM1 are mailbox SMs — leave them with n_pdos=0 and EC_WD_DEFAULT.
    // SM2 (RxPDO) and SM3 (TxPDO) carry the process data.
    let mut rxpdo_entries: Vec<ec_pdo_entry_info_t> = SV660N_RXPDO_ENTRIES
        .iter()
        .map(|&(index, subindex, bit_length)| ec_pdo_entry_info_t {
            index,
            subindex,
            bit_length,
        })
        .collect();

    let mut txpdo_entries: Vec<ec_pdo_entry_info_t> = SV660N_TXPDO_ENTRIES
        .iter()
        .map(|&(index, subindex, bit_length)| ec_pdo_entry_info_t {
            index,
            subindex,
            bit_length,
        })
        .collect();

    let mut rxpdo = ec_pdo_info_t {
        index: 0x1600,
        n_entries: rxpdo_entries.len() as u32,
        entries: rxpdo_entries.as_mut_ptr(),
    };

    let mut txpdo = ec_pdo_info_t {
        index: 0x1A00,
        n_entries: txpdo_entries.len() as u32,
        entries: txpdo_entries.as_mut_ptr(),
    };

    let syncs = [
        ec_sync_info_t {
            index: 0,
            dir: ec_direction_t::EC_DIR_OUTPUT,
            n_pdos: 0,
            pdos: std::ptr::null_mut(),
            watchdog_mode: ec_watchdog_mode_t::EC_WD_DEFAULT,
        },
        ec_sync_info_t {
            index: 1,
            dir: ec_direction_t::EC_DIR_INPUT,
            n_pdos: 0,
            pdos: std::ptr::null_mut(),
            watchdog_mode: ec_watchdog_mode_t::EC_WD_DEFAULT,
        },
        ec_sync_info_t {
            index: 2,
            dir: ec_direction_t::EC_DIR_OUTPUT,
            n_pdos: 1,
            pdos: &mut rxpdo,
            watchdog_mode: ec_watchdog_mode_t::EC_WD_DEFAULT,
        },
        ec_sync_info_t {
            index: 3,
            dir: ec_direction_t::EC_DIR_INPUT,
            n_pdos: 1,
            pdos: &mut txpdo,
            watchdog_mode: ec_watchdog_mode_t::EC_WD_DEFAULT,
        },
        // Terminator
        ec_sync_info_t {
            index: EC_END,
            dir: ec_direction_t::EC_DIR_INVALID,
            n_pdos: 0,
            pdos: std::ptr::null_mut(),
            watchdog_mode: ec_watchdog_mode_t::EC_WD_DEFAULT,
        },
    ];

    let ret = unsafe { ecrt_slave_config_pdos(sc, 5, syncs.as_ptr()) };
    if ret != 0 {
        bail!(
            "ecrt_slave_config_pdos() failed for slave {}: {}",
            slave.index,
            ret
        );
    }

    // Register ALL PDO entries in the domain.
    // IgH only maps entries that are explicitly registered; unregistered entries
    // cause FMMU size mismatch → "Invalid input configuration".
    let mut output_offset: u32 = 0;
    for (i, &(index, subindex, bits)) in SV660N_RXPDO_ENTRIES.iter().enumerate() {
        let ret = unsafe {
            ecrt_slave_config_reg_pdo_entry(
                sc,
                index,
                subindex,
                domain,
                std::ptr::null_mut(),
            )
        };
        if ret < 0 {
            bail!(
                "ecrt_slave_config_reg_pdo_entry(RxPDO[{}] 0x{:04X}:{:02X}) failed for slave {}: {}",
                i, index, subindex, slave.index, ret
            );
        }
        tracing::debug!(
            "RxPDO[{}] 0x{:04X}:{:02X} {}bit → domain offset {}",
            i, index, subindex, bits, ret
        );
        if i == 0 {
            output_offset = ret as u32;
        }
    }

    let mut input_offset: u32 = 0;
    for (i, &(index, subindex, bits)) in SV660N_TXPDO_ENTRIES.iter().enumerate() {
        let ret = unsafe {
            ecrt_slave_config_reg_pdo_entry(
                sc,
                index,
                subindex,
                domain,
                std::ptr::null_mut(),
            )
        };
        if ret < 0 {
            bail!(
                "ecrt_slave_config_reg_pdo_entry(TxPDO[{}] 0x{:04X}:{:02X}) failed for slave {}: {}",
                i, index, subindex, slave.index, ret
            );
        }
        tracing::debug!(
            "TxPDO[{}] 0x{:04X}:{:02X} {}bit → domain offset {}",
            i, index, subindex, bits, ret
        );
        if i == 0 {
            input_offset = ret as u32;
        }
    }

    info!(
        position = slave.index,
        output_offset, input_offset,
        "PDO offsets registered"
    );

    // Configure SDO startup parameters for PP mode.
    // These are written during slave configuration (PREOP→SAFEOP transition).
    unsafe {
        // Profile acceleration (0x6083:00) — units: counts/s²
        let ret = ecrt_slave_config_sdo32(sc, 0x6083, 0, 100_000);
        if ret != 0 {
            warn!(position = slave.index, ret, "Failed to set profile acceleration (0x6083)");
        }
        // Profile deceleration (0x6084:00)
        let ret = ecrt_slave_config_sdo32(sc, 0x6084, 0, 100_000);
        if ret != 0 {
            warn!(position = slave.index, ret, "Failed to set profile deceleration (0x6084)");
        }
        // Max profile velocity (0x607F:00)
        let ret = ecrt_slave_config_sdo32(sc, 0x607F, 0, 500_000);
        if ret != 0 {
            warn!(position = slave.index, ret, "Failed to set max profile velocity (0x607F)");
        }
    }
    info!(
        position = slave.index,
        "SDO startup config: accel=100000, decel=100000, max_vel=500000"
    );

    // Configure DC SYNC0
    unsafe {
        ecrt_slave_config_dc(
            sc,
            DC_ASSIGN_ACTIVATE_SYNC0,
            CYCLE_NS,
            0,    // sync0 shift
            0,    // sync1 cycle (unused)
            0,    // sync1 shift (unused)
        );
    }
    info!(
        position = slave.index,
        assign_activate = DC_ASSIGN_ACTIVATE_SYNC0,
        sync0_cycle_ns = CYCLE_NS,
        "DC SYNC0 configured"
    );

    runtimes.push(SlaveRuntime {
        output_offset: output_offset as usize,
        input_offset: input_offset as usize,
        kind: DeviceKind::MotionAxis,
        position: slave.index,
    });

    Ok(())
}

// ---------------------------------------------------------------------------
// SV660N axis tick — translates between the SV660N PDO layout and the
// generic MotionAxis interface
// ---------------------------------------------------------------------------

fn tick_axis_sv660n(
    axis: &mut crate::device::motion_axis::MotionAxis,
    input_pdo: &[u8],   // TxPDO from slave (SV660N layout)
    output_pdo: &mut [u8], // RxPDO to slave (SV660N layout)
) {
    // Build a "generic CiA402" input buffer for MotionAxis::tick():
    //   offset 0: statusword (u16)
    //   offset 2: actual_position (i32)
    let mut generic_input = [0u8; 6];
    // statusword is at OFF_TX_STATUSWORD in the SV660N TxPDO
    generic_input[0..2].copy_from_slice(&input_pdo[OFF_TX_STATUSWORD..OFF_TX_STATUSWORD + 2]);
    // actual_position is at OFF_TX_ACTUAL_POSITION
    generic_input[2..6].copy_from_slice(&input_pdo[OFF_TX_ACTUAL_POSITION..OFF_TX_ACTUAL_POSITION + 4]);

    // Prepare a generic output buffer:
    //   offset 0: controlword (u16)
    //   offset 2: target_position (i32)
    //   offset 6: profile_velocity (u32)
    let mut generic_output = [0u8; 10];

    axis.tick(&generic_input, &mut generic_output);

    // Map generic output back to SV660N RxPDO layout
    // controlword → OFF_RX_CONTROLWORD
    output_pdo[OFF_RX_CONTROLWORD..OFF_RX_CONTROLWORD + 2]
        .copy_from_slice(&generic_output[0..2]);
    // target_position → OFF_RX_TARGET_POSITION
    output_pdo[OFF_RX_TARGET_POSITION..OFF_RX_TARGET_POSITION + 4]
        .copy_from_slice(&generic_output[2..6]);
    // profile_velocity → OFF_RX_PROFILE_VELOCITY
    output_pdo[OFF_RX_PROFILE_VELOCITY..OFF_RX_PROFILE_VELOCITY + 4]
        .copy_from_slice(&generic_output[6..10]);

    // Fill PDO fields that we don't dynamically control but must not be zero,
    // because the PDO overwrites SDO values every cycle.
    output_pdo[OFF_RX_MAX_PROFILE_VEL..OFF_RX_MAX_PROFILE_VEL + 4]
        .copy_from_slice(&500_000u32.to_le_bytes());   // 0x607F max profile velocity
    output_pdo[OFF_RX_PROFILE_ACCEL..OFF_RX_PROFILE_ACCEL + 4]
        .copy_from_slice(&100_000u32.to_le_bytes());   // 0x6083 profile acceleration
    output_pdo[OFF_RX_PROFILE_DECEL..OFF_RX_PROFILE_DECEL + 4]
        .copy_from_slice(&100_000u32.to_le_bytes());   // 0x6084 profile deceleration

    // Set modes of operation to PP mode (1)
    output_pdo[OFF_RX_MODES_OF_OP] = 1;

    // --- Diagnostic: log every cycle when controlword has NEW_SETPOINT, or every 1s idle ---
    {
        use std::sync::atomic::{AtomicU32, Ordering};
        static DIAG_COUNTER: AtomicU32 = AtomicU32::new(0);
        static MOTION_LOG_COUNT: AtomicU32 = AtomicU32::new(0);
        let cnt = DIAG_COUNTER.fetch_add(1, Ordering::Relaxed);

        let sw = u16::from_le_bytes([input_pdo[OFF_TX_STATUSWORD], input_pdo[OFF_TX_STATUSWORD + 1]]);
        let cw = u16::from_le_bytes([generic_output[0], generic_output[1]]);
        let cw_actual = u16::from_le_bytes([
            output_pdo[OFF_RX_CONTROLWORD],
            output_pdo[OFF_RX_CONTROLWORD + 1],
        ]);
        let pos = i32::from_le_bytes([
            input_pdo[OFF_TX_ACTUAL_POSITION],
            input_pdo[OFF_TX_ACTUAL_POSITION + 1],
            input_pdo[OFF_TX_ACTUAL_POSITION + 2],
            input_pdo[OFF_TX_ACTUAL_POSITION + 3],
        ]);
        let target = i32::from_le_bytes([
            output_pdo[OFF_RX_TARGET_POSITION],
            output_pdo[OFF_RX_TARGET_POSITION + 1],
            output_pdo[OFF_RX_TARGET_POSITION + 2],
            output_pdo[OFF_RX_TARGET_POSITION + 3],
        ]);

        // Log every cycle when cw != 0x000F (motion active), up to 20 times
        if cw != 0x000F {
            let n = MOTION_LOG_COUNT.fetch_add(1, Ordering::Relaxed);
            if n < 20 {
                info!(
                    sw = format_args!("0x{:04X}", sw),
                    cw_gen = format_args!("0x{:04X}", cw),
                    cw_pdo = format_args!("0x{:04X}", cw_actual),
                    pos, target,
                    "MOTION PDO"
                );
            }
        } else if cnt % 2000 == 0 {
            // Idle: log every 2s
            info!(
                sw = format_args!("0x{:04X}", sw),
                cw = format_args!("0x{:04X}", cw),
                pos,
                "PDO idle"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Slave classification (same heuristic as before)
// ---------------------------------------------------------------------------

fn classify_slave_for_igh(slave: &SlaveInfo) -> Option<DeviceKind> {
    let name_lower = slave.name.to_lowercase();

    if name_lower.contains("sv660") || name_lower.contains("servo") {
        return Some(DeviceKind::MotionAxis);
    }
    if name_lower.contains("stf05") || name_lower.contains("stepper") || name_lower.contains("moons") {
        return Some(DeviceKind::MotionAxis);
    }
    if name_lower.contains("ec3a") || name_lower.contains("io16") || name_lower.contains("io module") {
        return Some(DeviceKind::IoModule);
    }
    if slave.has_cia402 {
        return Some(DeviceKind::MotionAxis);
    }

    None
}
