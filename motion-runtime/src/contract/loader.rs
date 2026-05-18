//! YAML loaders for startup config and contract files.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

use super::types::Contract;

/// Top-level startup config. Read via `motion-runtime --config path/to/x.yaml`.
///
/// Kept minimal on purpose — additional sections (ethercat / grpc / logging)
/// can be added later under their own keys; serde will ignore unknown keys
/// unless we enable strict mode.
#[derive(Debug, Clone, Deserialize)]
pub struct StartupConfig {
    /// List of contract YAML files to load. Each path is resolved
    /// relative to the directory containing the startup config file.
    pub contracts: Vec<PathBuf>,
}

/// Load and parse the startup config YAML.
///
/// Returns the parsed config and the absolute directory the config
/// itself lives in — used as the base for resolving contract paths.
pub fn load_config(config_path: &Path) -> Result<(StartupConfig, PathBuf)> {
    let config_path = config_path
        .canonicalize()
        .with_context(|| format!("config file not found: {}", config_path.display()))?;
    let text = fs::read_to_string(&config_path)
        .with_context(|| format!("failed to read config: {}", config_path.display()))?;
    let cfg: StartupConfig = serde_yaml::from_str(&text)
        .with_context(|| format!("failed to parse config YAML: {}", config_path.display()))?;
    let base_dir = config_path
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    Ok((cfg, base_dir))
}

/// Load and parse a single contract YAML file.
pub fn load_contract(contract_path: &Path) -> Result<Contract> {
    let text = fs::read_to_string(contract_path)
        .with_context(|| format!("failed to read contract: {}", contract_path.display()))?;
    let contract: Contract = serde_yaml::from_str(&text)
        .with_context(|| format!("failed to parse contract: {}", contract_path.display()))?;
    Ok(contract)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::types::{FieldDir, FieldType};
    use std::io::Write;

    fn write_temp(name: &str, content: &str) -> PathBuf {
        let dir = std::env::temp_dir();
        let path = dir.join(name);
        let mut f = fs::File::create(&path).unwrap();
        f.write_all(content.as_bytes()).unwrap();
        path
    }

    #[test]
    fn parses_minimal_contract() {
        let yaml = r#"
device: arm
description: "test"
protocol_version: 1
slave_match:
  name_contains: "EPSON RC90"
pdo_mapping:
  rx_pdo_index: 0x1600
  rx_pdo_size: 32
  tx_pdo_index: 0x1A00
  tx_pdo_size: 16
fields:
  target_x:
    offset: 0
    type: f32
    dir: out
  trigger:
    offset: 19
    type: bool
    dir: out
    bit: 0
  done:
    offset: 0
    type: bool
    dir: in
    bit: 0
"#;
        let path = write_temp("contract_minimal_test.yaml", yaml);
        let c = load_contract(&path).unwrap();
        assert_eq!(c.device, "arm");
        assert_eq!(c.protocol_version, 1);
        assert_eq!(c.pdo_mapping.rx_pdo_size, 32);
        assert_eq!(c.pdo_mapping.tx_pdo_size, 16);

        let target_x = c.fields.get("target_x").unwrap();
        assert_eq!(target_x.offset, 0);
        assert_eq!(target_x.field_type, FieldType::F32);
        assert_eq!(target_x.dir, FieldDir::Out);
        assert!(target_x.bit.is_none());

        let trigger = c.fields.get("trigger").unwrap();
        assert_eq!(trigger.bit, Some(0));
        assert_eq!(trigger.field_type, FieldType::Bool);

        let _ = fs::remove_file(&path);
    }

    #[test]
    fn parses_startup_config() {
        let yaml = r#"
contracts:
  - contracts/arm/epson-rc90b/contract.yaml
  - contracts/io/beckhoff-ek1100/contract.yaml
"#;
        let path = write_temp("startup_config_test.yaml", yaml);
        let (cfg, _) = load_config(&path).unwrap();
        assert_eq!(cfg.contracts.len(), 2);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn parses_motion_routines_section() {
        let yaml = r#"
device: arm
description: ""
protocol_version: 3
slave_match: {}
pdo_mapping:
  rx_pdo_index: 0x1600
  rx_pdo_size: 32
  tx_pdo_index: 0x1A00
  tx_pdo_size: 16
fields:
  target_x:
    offset: 0
    type: f32
    dir: out
motion_routines:
  GO: 1
  JUMP: 2
  LINEAR: 3
  HOME: 4
"#;
        let path = write_temp("contract_routines_test.yaml", yaml);
        let c = load_contract(&path).unwrap();
        assert_eq!(c.motion_routines.get("GO"), Some(&1));
        assert_eq!(c.motion_routines.get("JUMP"), Some(&2));
        assert_eq!(c.motion_routines.get("LINEAR"), Some(&3));
        assert_eq!(c.motion_routines.get("HOME"), Some(&4));
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn parses_real_ls6_contract_file() {
        // The actual contract.yaml shipped under contracts/arm/epson-rc90b/
        // must load cleanly — protects against schema drift in either
        // direction (rust types vs yaml file).
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("contracts")
            .join("arm")
            .join("epson-rc90b")
            .join("contract.yaml");
        let c = load_contract(&path).unwrap();
        assert_eq!(c.device, "arm");
        assert_eq!(c.protocol_version, 3);
        // motion_routines must be present and have the four LS6 motions.
        assert_eq!(c.motion_routines.get("LINEAR"), Some(&3));
        assert_eq!(c.motion_routines.get("HOME"), Some(&4));
    }
}
