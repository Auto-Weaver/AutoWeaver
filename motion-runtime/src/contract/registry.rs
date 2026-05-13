//! ContractRegistry — central read-only index of loaded contracts.
//!
//! Loaded once at startup, then queried (lock-free) by both the gRPC path
//! and the EtherCAT loop.

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{anyhow, Result};

use super::types::{Contract, FieldSpec};

/// Read-only registry of loaded contracts, keyed by their `device` logical name.
///
/// After `seal()` is called (typically right after startup loading), the
/// registry is immutable and can be shared via `Arc` to any number of readers.
#[derive(Debug, Default)]
pub struct ContractRegistry {
    by_device: HashMap<String, Arc<ContractEntry>>,
}

/// A loaded contract plus its runtime binding (which slave it ended up on).
#[derive(Debug)]
pub struct ContractEntry {
    pub contract: Contract,
    /// EtherCAT slave position (0..N-1) this contract is bound to.
    /// `None` before bus scan / binding.
    pub slave_position: Option<u16>,
}

impl ContractRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Insert a contract. Errors if the same device name is already taken —
    /// device names are required to be unique within a single startup config.
    pub fn insert(&mut self, contract: Contract) -> Result<()> {
        if self.by_device.contains_key(&contract.device) {
            return Err(anyhow!(
                "duplicate device name in contracts: {}",
                contract.device
            ));
        }
        self.by_device.insert(
            contract.device.clone(),
            Arc::new(ContractEntry {
                contract,
                slave_position: None,
            }),
        );
        Ok(())
    }

    /// Bind a device to a scanned slave position. Called after bus scan
    /// resolves which slave each contract's `slave_match` selected.
    pub fn bind_slave(&mut self, device: &str, slave_position: u16) -> Result<()> {
        let entry = self
            .by_device
            .get_mut(device)
            .ok_or_else(|| anyhow!("device not found: {}", device))?;
        // We are still in mutation phase, so unwrap_or_clone is safe.
        let mut new_entry = ContractEntry {
            contract: entry.contract.clone(),
            slave_position: Some(slave_position),
        };
        new_entry.slave_position = Some(slave_position);
        *entry = Arc::new(new_entry);
        Ok(())
    }

    /// Look up a contract by device logical name.
    pub fn get(&self, device: &str) -> Result<Arc<ContractEntry>> {
        self.by_device
            .get(device)
            .cloned()
            .ok_or_else(|| anyhow!("unknown device: {}", device))
    }

    /// Look up a specific field. Convenience wrapper around `get` + field map.
    pub fn field<'a>(&'a self, device: &str, field: &str) -> Result<(Arc<ContractEntry>, FieldSpec)> {
        let entry = self.get(device)?;
        let spec = entry
            .contract
            .fields
            .get(field)
            .cloned()
            .ok_or_else(|| anyhow!("unknown field on device {}: {}", device, field))?;
        Ok((entry, spec))
    }

    /// Iterate all entries (for the binding phase).
    pub fn iter(&self) -> impl Iterator<Item = (&str, &Arc<ContractEntry>)> {
        self.by_device
            .iter()
            .map(|(k, v)| (k.as_str(), v))
    }

    pub fn len(&self) -> usize {
        self.by_device.len()
    }

    pub fn is_empty(&self) -> bool {
        self.by_device.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::types::{FieldDir, FieldType, PdoMapping, SlaveMatch};

    fn mk_contract(device: &str) -> Contract {
        let mut fields = std::collections::BTreeMap::new();
        fields.insert(
            "target_x".to_string(),
            FieldSpec {
                offset: 0,
                field_type: FieldType::F32,
                dir: FieldDir::Out,
                bit: None,
            },
        );
        Contract {
            device: device.to_string(),
            description: String::new(),
            protocol_version: 1,
            slave_match: SlaveMatch::default(),
            pdo_mapping: PdoMapping {
                rx_pdo_index: 0x1600,
                rx_pdo_size: 32,
                tx_pdo_index: 0x1A00,
                tx_pdo_size: 16,
            },
            fields,
        }
    }

    #[test]
    fn insert_and_query() {
        let mut reg = ContractRegistry::new();
        reg.insert(mk_contract("arm")).unwrap();
        let entry = reg.get("arm").unwrap();
        assert_eq!(entry.contract.device, "arm");
        assert!(entry.slave_position.is_none());
    }

    #[test]
    fn duplicate_device_rejected() {
        let mut reg = ContractRegistry::new();
        reg.insert(mk_contract("arm")).unwrap();
        let err = reg.insert(mk_contract("arm")).unwrap_err();
        assert!(err.to_string().contains("duplicate"));
    }

    #[test]
    fn field_lookup() {
        let mut reg = ContractRegistry::new();
        reg.insert(mk_contract("arm")).unwrap();
        let (_, spec) = reg.field("arm", "target_x").unwrap();
        assert_eq!(spec.field_type, FieldType::F32);
        let err = reg.field("arm", "no_such_field").unwrap_err();
        assert!(err.to_string().contains("unknown field"));
    }

    #[test]
    fn bind_slave_updates_entry() {
        let mut reg = ContractRegistry::new();
        reg.insert(mk_contract("arm")).unwrap();
        reg.bind_slave("arm", 3).unwrap();
        assert_eq!(reg.get("arm").unwrap().slave_position, Some(3));
    }
}
