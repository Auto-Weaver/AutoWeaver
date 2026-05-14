//! EtherCAT master loop driven by the IgH userspace library.
//!
//! Only the `igh` Cargo feature compiles the real loop; without it `run`
//! returns an informative error so a dev machine without IgH installed can
//! still `cargo build` / `cargo test` and exercise the gRPC + translation
//! layer against in-memory PDO buffers.
//!
//! ## Loop shape (when `igh` is on)
//!
//! 1. `ecrt_request_master(0)` to obtain the master handle.
//! 2. Scan the bus, enumerate each slave's vendor / product / name.
//! 3. For every scanned slave, look up a contract whose `slave_match`
//!    accepts it. Slaves with no matching contract are logged and skipped.
//! 4. For each matched slave, configure SM0..3 (mailbox SMs empty, SM2 =
//!    RxPDO, SM3 = TxPDO) with the contract's `pdo_mapping`. Each PDO is
//!    materialized as a flat array of `rx_pdo_size` × USINT byte entries at
//!    `0x2100:1..N` and `tx_pdo_size` × USINT byte entries at `0x2000:1..N`
//!    — matching what the Epson RC90-B (and any other byte-granular
//!    option board) exposes.
//! 5. Register every PDO entry in the EtherCAT domain so IgH gives us a
//!    byte offset into the process-data buffer for each slave.
//! 6. Activate the master, pump cycles until slaves reach SAFEOP+.
//! 7. Enter the 1 ms cyclic loop: receive → process → pull each slave's
//!    TxPDO bytes into `PdoBuffers` → push each slave's RxPDO bytes out →
//!    queue → send.
//!
//! ## Why no DC SYNC
//!
//! The RC90-B option board's ESI declares no `<Dc>` section and supports
//! "delay measurement only". DC SYNC is unnecessary and would only burn
//! cycles. For motor drives that do need SYNC (e.g. SV660N), bring them
//! back in via a separate slave-type branch. See
//! `docs/research/cia402-protocol-notes.md`.

#[cfg(not(feature = "igh"))]
pub async fn run(
    _interface: &str,
    _contracts: std::sync::Arc<crate::contract::ContractRegistry>,
    _buffers: std::sync::Arc<crate::ethercat::PdoBuffers>,
) -> anyhow::Result<()> {
    tracing::warn!(
        "motion-runtime built without --features igh — EtherCAT bus driver \
         disabled. Rebuild on a host with IgH installed: \
         `cargo build --features igh`. gRPC remains online; writes land in \
         in-memory PDO buffers only."
    );
    anyhow::bail!("ethercat master compiled without `igh` feature")
}

#[cfg(feature = "igh")]
pub use igh_impl::run;

#[cfg(feature = "igh")]
mod igh_impl {
    use std::ptr;
    use std::sync::Arc;
    use std::time::Duration;

    use anyhow::{bail, Context, Result};
    use tokio::time::MissedTickBehavior;
    use tracing::{debug, info, warn};

    use crate::contract::{Contract, ContractRegistry};
    use crate::ethercat::igh_ffi::*;
    use crate::ethercat::PdoBuffers;

    /// Cyclic loop period — 1 ms is the industry default for non-DC slaves.
    /// The RC90-B option board does not require deterministic SYNC, so we
    /// can stay on tokio's `interval` (~µs jitter) instead of a real-time
    /// thread.
    const CYCLE_US: u64 = 1000;

    /// `/dev/EtherCAT0` is the only master we ever request.
    const MASTER_INDEX: u32 = 0;

    /// EtherCAT AL-state bitmasks. See `ec_master_state_t::al_states()`.
    ///
    /// Note: per the EtherCAT spec these are state *values* (an enum), not
    /// independent bit flags — INIT=1, PREOP=2, SAFEOP=4, OP=8. IgH reports
    /// `al_states` as the OR across all slaves, so for a single-slave bus
    /// the field directly equals the slave's current state. Our "reached
    /// SAFEOP or higher" check therefore tests for either SAFEOP or OP
    /// bits being set; matching only SAFEOP would falsely fail if the
    /// slave hopped straight to OP (which RC90-B does — no SDO config so
    /// no reason to dwell in SAFEOP).
    const AL_STATE_SAFEOP: u32 = 0x04;
    const AL_STATE_OP: u32 = 0x08;
    const AL_STATE_SAFEOP_OR_OP: u32 = AL_STATE_SAFEOP | AL_STATE_OP;

    /// How long to pump cycles waiting for slaves to leave PREOP.
    const SAFEOP_TIMEOUT: Duration = Duration::from_secs(10);

    /// Slave runtime info captured once at startup and consulted every
    /// cycle to know where to memcpy bytes.
    struct SlaveRuntime {
        position: u16,
        device: String,
        rx_size: usize,
        tx_size: usize,
        /// Byte offset of this slave's RxPDO image (master → slave) in the
        /// shared IgH domain buffer.
        output_offset: usize,
        /// Byte offset of this slave's TxPDO image (slave → master).
        input_offset: usize,
    }

    pub async fn run(
        _interface: &str,
        contracts: Arc<ContractRegistry>,
        buffers: Arc<PdoBuffers>,
    ) -> Result<()> {
        info!(
            "initializing IgH EtherCAT master {} (cycle = {} µs)",
            MASTER_INDEX, CYCLE_US
        );

        let master = unsafe { ecrt_request_master(MASTER_INDEX) };
        if master.is_null() {
            bail!(
                "ecrt_request_master({}) returned null — is the kernel module \
                 loaded? (sudo systemctl start ethercat) and is the user in \
                 the `ethercat` group?",
                MASTER_INDEX
            );
        }

        let mut master_info: ec_master_info_t = unsafe { std::mem::zeroed() };
        if unsafe { ecrt_master(master, &mut master_info) } != 0 {
            bail!("ecrt_master() failed");
        }
        let slave_count = master_info.slave_count;
        info!(slave_count, "bus scan complete");
        if slave_count == 0 {
            bail!("no slaves found on the bus");
        }

        let domain = unsafe { ecrt_master_create_domain(master) };
        if domain.is_null() {
            bail!("ecrt_master_create_domain() failed");
        }

        let runtimes = match_and_configure_slaves(master, domain, slave_count, &contracts)?;
        if runtimes.is_empty() {
            bail!("no scanned slaves matched any loaded contract");
        }

        unsafe { ecrt_master_set_send_interval(master, CYCLE_US as usize) };
        if unsafe { ecrt_master_activate(master) } != 0 {
            bail!("ecrt_master_activate() failed");
        }

        let domain_data = unsafe { ecrt_domain_data(domain) };
        if domain_data.is_null() {
            bail!("ecrt_domain_data() returned null");
        }
        let domain_size = unsafe { ecrt_domain_size(domain) };
        info!(domain_size, "master activated, domain mapped");

        wait_for_safeop(master, domain).await?;

        info!("entering cyclic loop");
        cyclic_loop(master, domain, domain_data, &runtimes, &buffers).await
    }

    /// For every scanned slave, find a matching contract and configure it.
    /// Returns one `SlaveRuntime` per successfully configured slave.
    fn match_and_configure_slaves(
        master: *mut ec_master_t,
        domain: *mut ec_domain_t,
        slave_count: u32,
        contracts: &Arc<ContractRegistry>,
    ) -> Result<Vec<SlaveRuntime>> {
        let mut runtimes = Vec::new();

        for position in 0..slave_count as u16 {
            let mut si: ec_slave_info_t = unsafe { std::mem::zeroed() };
            if unsafe { ecrt_master_get_slave(master, position, &mut si) } != 0 {
                warn!(position, "ecrt_master_get_slave failed; skipping");
                continue;
            }

            let name = {
                let len = si.name.iter().position(|&b| b == 0).unwrap_or(si.name.len());
                String::from_utf8_lossy(&si.name[..len]).to_string()
            };

            let matched = contracts.iter().find(|(_, entry)| {
                let sm = &entry.contract.slave_match;
                sm.vendor_id == Some(si.vendor_id)
                    && sm.product_code == Some(si.product_code)
            });

            let Some((device, entry)) = matched else {
                info!(
                    position,
                    name = %name,
                    vendor = format!("0x{:08X}", si.vendor_id),
                    product = format!("0x{:08X}", si.product_code),
                    "scanned slave matches no contract — skipping"
                );
                continue;
            };

            // main.rs binds devices to synthetic slave positions (insertion
            // order). For a single slave that's always 0 and matches the bus.
            // For multi-slave the user must order dev.yaml to match physical
            // bus order — surface a clear error if they didn't.
            if let Some(bound) = entry.slave_position {
                if bound != position {
                    bail!(
                        "slave position mismatch for device '{}': contract bound \
                         to position {} (from dev.yaml insertion order) but found \
                         on bus at position {}. Reorder dev.yaml contracts to match \
                         physical bus order.",
                        device, bound, position
                    );
                }
            }

            info!(
                position,
                device = device,
                name = %name,
                "configuring slave from contract"
            );

            let (output_offset, input_offset) =
                configure_slave(master, domain, position, &entry.contract)?;

            runtimes.push(SlaveRuntime {
                position,
                device: device.to_string(),
                rx_size: entry.contract.pdo_mapping.rx_pdo_size,
                tx_size: entry.contract.pdo_mapping.tx_pdo_size,
                output_offset,
                input_offset,
            });
        }

        Ok(runtimes)
    }

    /// Configure one slave's PDO mapping and register its entries in the
    /// domain. Returns the byte offsets of the slave's output (RxPDO) and
    /// input (TxPDO) images within the domain buffer.
    fn configure_slave(
        master: *mut ec_master_t,
        domain: *mut ec_domain_t,
        position: u16,
        contract: &Contract,
    ) -> Result<(usize, usize)> {
        let vendor_id = contract
            .slave_match
            .vendor_id
            .context("contract.slave_match.vendor_id is required for IgH binding")?;
        let product_code = contract
            .slave_match
            .product_code
            .context("contract.slave_match.product_code is required for IgH binding")?;

        let sc = unsafe { ecrt_master_slave_config(master, 0, position, vendor_id, product_code) };
        if sc.is_null() {
            bail!("ecrt_master_slave_config failed for position {}", position);
        }

        let rx_idx = contract.pdo_mapping.rx_pdo_index;
        let tx_idx = contract.pdo_mapping.tx_pdo_index;
        let rx_size = contract.pdo_mapping.rx_pdo_size;
        let tx_size = contract.pdo_mapping.tx_pdo_size;

        // USINT-byte mapping: each entry is one subindex of 0x2100 (output)
        // or 0x2000 (input). The board exposes up to 128 subindices per
        // PDO (largest single PDO assembly is 0x1602 = 128 B = subindex
        // 0x80). 256-byte PDOs use two stacked PDOs, which we'd handle by
        // breaking into separate sync entries — out of scope for now.
        if rx_size > 0xFF || tx_size > 0xFF {
            bail!(
                "PDO size > 255 bytes not yet supported (rx={}, tx={}) — \
                 would need multi-PDO sync layout",
                rx_size,
                tx_size
            );
        }

        // The entry arrays MUST live until after ecrt_slave_config_pdos
        // returns, because the sync_info_t holds raw pointers into them.
        let mut rxpdo_entries: Vec<ec_pdo_entry_info_t> = (1..=rx_size)
            .map(|sub| ec_pdo_entry_info_t {
                index: 0x2100,
                subindex: sub as u8,
                bit_length: 8,
            })
            .collect();
        let mut txpdo_entries: Vec<ec_pdo_entry_info_t> = (1..=tx_size)
            .map(|sub| ec_pdo_entry_info_t {
                index: 0x2000,
                subindex: sub as u8,
                bit_length: 8,
            })
            .collect();

        let mut rxpdo = ec_pdo_info_t {
            index: rx_idx,
            n_entries: rx_size as u32,
            entries: rxpdo_entries.as_mut_ptr(),
        };
        let mut txpdo = ec_pdo_info_t {
            index: tx_idx,
            n_entries: tx_size as u32,
            entries: txpdo_entries.as_mut_ptr(),
        };

        let syncs = [
            ec_sync_info_t {
                index: 0,
                dir: ec_direction_t::EC_DIR_OUTPUT,
                n_pdos: 0,
                pdos: ptr::null_mut(),
                watchdog_mode: ec_watchdog_mode_t::EC_WD_DEFAULT,
            },
            ec_sync_info_t {
                index: 1,
                dir: ec_direction_t::EC_DIR_INPUT,
                n_pdos: 0,
                pdos: ptr::null_mut(),
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
            ec_sync_info_t {
                index: EC_END,
                dir: ec_direction_t::EC_DIR_INVALID,
                n_pdos: 0,
                pdos: ptr::null_mut(),
                watchdog_mode: ec_watchdog_mode_t::EC_WD_DEFAULT,
            },
        ];

        if unsafe { ecrt_slave_config_pdos(sc, 5, syncs.as_ptr()) } != 0 {
            bail!("ecrt_slave_config_pdos failed for position {}", position);
        }

        let mut output_offset = 0i32;
        for sub in 1..=rx_size {
            let off = unsafe {
                ecrt_slave_config_reg_pdo_entry(sc, 0x2100, sub as u8, domain, ptr::null_mut())
            };
            if off < 0 {
                bail!(
                    "ecrt_slave_config_reg_pdo_entry RxPDO 0x2100:{:02X} failed for \
                     position {}: {}",
                    sub,
                    position,
                    off
                );
            }
            if sub == 1 {
                output_offset = off;
            }
        }

        let mut input_offset = 0i32;
        for sub in 1..=tx_size {
            let off = unsafe {
                ecrt_slave_config_reg_pdo_entry(sc, 0x2000, sub as u8, domain, ptr::null_mut())
            };
            if off < 0 {
                bail!(
                    "ecrt_slave_config_reg_pdo_entry TxPDO 0x2000:{:02X} failed for \
                     position {}: {}",
                    sub,
                    position,
                    off
                );
            }
            if sub == 1 {
                input_offset = off;
            }
        }

        debug!(
            position,
            output_offset, input_offset, rx_size, tx_size, "PDO entries registered"
        );

        Ok((output_offset as usize, input_offset as usize))
    }

    /// Pump cycles until the bus reaches SAFEOP+, or bail after timeout.
    /// During this phase we ignore the gRPC-side buffers — outputs are
    /// zero, which the SPEL+ program is required to tolerate (it waits for
    /// `trigger` to go high before doing anything).
    async fn wait_for_safeop(master: *mut ec_master_t, domain: *mut ec_domain_t) -> Result<()> {
        let deadline = tokio::time::Instant::now() + SAFEOP_TIMEOUT;
        let mut interval = tokio::time::interval(Duration::from_micros(CYCLE_US));
        interval.set_missed_tick_behavior(MissedTickBehavior::Skip);

        loop {
            interval.tick().await;
            unsafe {
                ecrt_master_receive(master);
                ecrt_domain_process(domain);
                ecrt_domain_queue(domain);
                ecrt_master_send(master);
            }

            let mut ms: ec_master_state_t = unsafe { std::mem::zeroed() };
            unsafe { ecrt_master_state(master, &mut ms) };
            if (ms.al_states() & AL_STATE_SAFEOP_OR_OP) != 0 {
                info!(
                    al_states = format!("0x{:02X}", ms.al_states()),
                    "slaves reached SAFEOP or OP"
                );
                return Ok(());
            }

            if tokio::time::Instant::now() >= deadline {
                bail!(
                    "timeout waiting for slaves to reach SAFEOP (current al_states = 0x{:02X}, \
                     link_up = {})",
                    ms.al_states(),
                    ms.link_up()
                );
            }
        }
    }

    /// Forever loop: shuffle bytes between `PdoBuffers` and the IgH domain.
    async fn cyclic_loop(
        master: *mut ec_master_t,
        domain: *mut ec_domain_t,
        domain_data: *mut u8,
        runtimes: &[SlaveRuntime],
        buffers: &Arc<PdoBuffers>,
    ) -> Result<()> {
        let mut interval = tokio::time::interval(Duration::from_micros(CYCLE_US));
        interval.set_missed_tick_behavior(MissedTickBehavior::Skip);

        let mut cycle: u64 = 0;
        let mut op_seen = false;

        loop {
            interval.tick().await;
            cycle = cycle.wrapping_add(1);

            unsafe {
                ecrt_master_receive(master);
                ecrt_domain_process(domain);
            }

            for sr in runtimes {
                let Ok(slave_bufs) = buffers.get(sr.position) else {
                    continue;
                };

                // TxPDO: domain → our buffer (latest inputs visible to gRPC reads).
                {
                    let mut tx_buf = slave_bufs.tx.lock().unwrap();
                    unsafe {
                        std::ptr::copy_nonoverlapping(
                            domain_data.add(sr.input_offset),
                            tx_buf.as_mut_ptr(),
                            sr.tx_size,
                        );
                    }
                }

                // RxPDO: our buffer → domain (this cycle's outputs go on the wire).
                {
                    let rx_buf = slave_bufs.rx.lock().unwrap();
                    unsafe {
                        std::ptr::copy_nonoverlapping(
                            rx_buf.as_ptr(),
                            domain_data.add(sr.output_offset),
                            sr.rx_size,
                        );
                    }
                }
            }

            unsafe {
                ecrt_domain_queue(domain);
                ecrt_master_send(master);
            }

            // Every ~2 s: peek master state. First time we see OP, log
            // a one-shot info; if we drop out of OP, log a warn.
            if cycle % 2000 == 0 {
                let mut ms: ec_master_state_t = unsafe { std::mem::zeroed() };
                unsafe { ecrt_master_state(master, &mut ms) };
                let in_op = (ms.al_states() & AL_STATE_OP) != 0;
                if in_op && !op_seen {
                    op_seen = true;
                    info!(
                        slaves = runtimes.len(),
                        devices = ?runtimes.iter().map(|r| &r.device).collect::<Vec<_>>(),
                        "all slaves reached OP — bus online"
                    );
                } else if !in_op && op_seen {
                    op_seen = false;
                    warn!(
                        al_states = format!("0x{:02X}", ms.al_states()),
                        "slaves dropped out of OP"
                    );
                }
                debug!(
                    cycle,
                    slaves_responding = ms.slaves_responding,
                    al_states = format!("0x{:02X}", ms.al_states()),
                    link_up = ms.link_up(),
                    "master state"
                );
            }
        }
    }
}
