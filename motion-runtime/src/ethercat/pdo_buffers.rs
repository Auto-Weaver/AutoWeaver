//! Shared PDO buffers between the gRPC path and the EtherCAT cyclic loop.
//!
//! Each bound slave has a fixed-size RxPDO output buffer (master → slave)
//! and TxPDO input buffer (slave → master). Sizes come from the contract's
//! `pdo_mapping`.
//!
//! - **gRPC path** writes into the RxPDO buffer (for `WriteField`) and reads
//!   from the TxPDO buffer (for `ReadField`).
//! - **EtherCAT cyclic loop** copies the RxPDO buffer onto the wire each
//!   cycle, then copies received TxPDO bytes back into the TxPDO buffer.
//!
//! Synchronization uses a `Mutex` per slave. The gRPC critical section is
//! tiny (single-field encode); the cyclic loop's critical section is one
//! memcpy of the whole buffer. With per-slave locks, gRPC writes to slave A
//! don't block gRPC writes to slave B, and the cyclic loop holds each lock
//! only briefly.

use std::collections::HashMap;
use std::sync::Mutex;

use anyhow::{anyhow, Result};

/// Read-write byte buffers for one slave.
#[derive(Debug)]
pub struct SlaveBuffers {
    /// Master → slave bytes. Length = contract's rx_pdo_size.
    pub rx: Mutex<Vec<u8>>,
    /// Slave → master bytes. Length = contract's tx_pdo_size.
    pub tx: Mutex<Vec<u8>>,
}

impl SlaveBuffers {
    pub fn new(rx_size: usize, tx_size: usize) -> Self {
        Self {
            rx: Mutex::new(vec![0u8; rx_size]),
            tx: Mutex::new(vec![0u8; tx_size]),
        }
    }
}

/// All slaves' PDO buffers, keyed by EtherCAT slave position (0..N-1).
///
/// Built once at startup after slave binding. Shared via `Arc` to gRPC
/// server and EtherCAT loop. The map itself is immutable after construction
/// — only the buffer contents inside each `SlaveBuffers` change.
#[derive(Debug, Default)]
pub struct PdoBuffers {
    by_slave: HashMap<u16, SlaveBuffers>,
}

impl PdoBuffers {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a slave's buffer sizes. Called during startup binding.
    pub fn insert(&mut self, slave_position: u16, rx_size: usize, tx_size: usize) {
        self.by_slave
            .insert(slave_position, SlaveBuffers::new(rx_size, tx_size));
    }

    /// Get the buffer pair for a slave.
    pub fn get(&self, slave_position: u16) -> Result<&SlaveBuffers> {
        self.by_slave
            .get(&slave_position)
            .ok_or_else(|| anyhow!("no PDO buffers for slave position {}", slave_position))
    }

    /// Iterate all bound slaves (for the cyclic loop's copy step).
    pub fn iter(&self) -> impl Iterator<Item = (u16, &SlaveBuffers)> {
        self.by_slave.iter().map(|(k, v)| (*k, v))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn isolated_slave_buffers() {
        let mut bufs = PdoBuffers::new();
        bufs.insert(0, 32, 16);
        bufs.insert(1, 8, 8);

        let s0 = bufs.get(0).unwrap();
        let s1 = bufs.get(1).unwrap();

        assert_eq!(s0.rx.lock().unwrap().len(), 32);
        assert_eq!(s0.tx.lock().unwrap().len(), 16);
        assert_eq!(s1.rx.lock().unwrap().len(), 8);
        assert_eq!(s1.tx.lock().unwrap().len(), 8);

        // Modify slave 0's rx; slave 1's must be unaffected.
        s0.rx.lock().unwrap()[5] = 0xAB;
        assert_eq!(s1.rx.lock().unwrap()[5], 0);
    }

    #[test]
    fn unknown_slave_errors() {
        let bufs = PdoBuffers::new();
        assert!(bufs.get(99).is_err());
    }
}
