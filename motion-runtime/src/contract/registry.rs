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

    /// Look up the routine number for a given motion enum on a device.
    /// `motion_name` is the enum name without the prefix, e.g. `"LINEAR"`,
    /// `"GO"`, `"JUMP"`, `"HOME"`. Errors if the device has no
    /// `motion_routines` table or the motion isn't in it.
    pub fn motion_routine(&self, device: &str, motion_name: &str) -> Result<u8> {
        let entry = self.get(device)?;
        entry
            .contract
            .motion_routines
            .get(motion_name)
            .copied()
            .ok_or_else(|| {
                anyhow!(
                    "device {} has no motion_routines entry for {}",
                    device,
                    motion_name
                )
            })
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
        let mut motion_routines = std::collections::BTreeMap::new();
        motion_routines.insert("LINEAR".to_string(), 3);
        motion_routines.insert("GO".to_string(), 1);
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
            motion_routines,
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

    #[test]
    fn motion_routine_lookup_returns_number() {
        let mut reg = ContractRegistry::new();
        reg.insert(mk_contract("arm")).unwrap();
        assert_eq!(reg.motion_routine("arm", "LINEAR").unwrap(), 3);
        assert_eq!(reg.motion_routine("arm", "GO").unwrap(), 1);
    }

    #[test]
    fn motion_routine_unknown_motion_errors() {
        let mut reg = ContractRegistry::new();
        reg.insert(mk_contract("arm")).unwrap();
        let err = reg.motion_routine("arm", "PIROUETTE").unwrap_err();
        assert!(err.to_string().contains("PIROUETTE"));
    }

    #[test]
    fn motion_routine_unknown_device_errors() {
        let reg = ContractRegistry::new();
        let err = reg.motion_routine("ghost", "GO").unwrap_err();
        assert!(err.to_string().contains("ghost"));
    }
}
