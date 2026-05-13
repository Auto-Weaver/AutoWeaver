//! EtherCAT master loop (skeleton).
//!
//! 0.7.0 phase 1: **stub**. This file is kept as the entry point that will be
//! filled in once the LS6-B602C option board is on the bench — at that point
//! the IgH configuration and cyclic loop will be written against the real ESI
//! and PDO layout.
//!
//! Current behavior: return an error so callers know the bus driver is not
//! up yet. The Cargo feature `igh` gates whether libethercat is linked
//! (default off, so dev machines without IgH installed can still run
//! `cargo build` / `cargo test`).
//!
//! What this file needs to do once the hardware is available (sketched in
//! the "Startup flow" section of 003):
//!
//! 1. `ecrt_request_master(0)` to obtain the master handle.
//! 2. Scan slaves; for each one, find the matching contract via `slave_match`.
//! 3. Configure RxPDO/TxPDO from the contract's `pdo_mapping`
//!    (rx_pdo_index/size + tx_pdo_index/size).
//! 4. Register PDO entries into the EtherCAT domain.
//! 5. Activate the master and wait for SAFEOP/OP.
//! 6. Enter the 1 ms cyclic loop:
//!    a. receive + domain_process
//!    b. copy each slave's RxPDO buffer into the corresponding domain region
//!    c. domain_queue + send
//!    d. copy the slave's TxPDO region from the domain back into its buffer
//!
//! Invariants that must hold across implementations:
//! - All PDO byte I/O goes through `PdoBuffers` as the single entry point.
//! - The cyclic loop is the only writer that sends bytes onto the wire; the
//!   gRPC path only mutates buffers.
//! - DC SYNC configuration must happen before `activate` (required by Inovance
//!   class devices; see the pitfalls doc).

use std::sync::Arc;

use anyhow::{bail, Result};
use tracing::warn;

use crate::contract::ContractRegistry;
use crate::ethercat::PdoBuffers;

/// Stub entry point for the EtherCAT master loop.
///
/// The real implementation lands when the LS6 hardware is available. Until
/// then this returns an error so callers know the bus did not actually
/// come up.
pub async fn run(
    _interface: &str,
    _contracts: Arc<ContractRegistry>,
    _buffers: Arc<PdoBuffers>,
) -> Result<()> {
    warn!(
        "ethercat::master::run() is a stub — bus driver lands when LS6 hardware \
         is available. See docs/evo/003-motion-runtime.md (section on 0.7.0 \
         refactor trade-offs)."
    );
    bail!("ethercat master not implemented in 0.7.0 phase 1 (skeleton-only)")
}
