//! Contract loading and field lookup.
//!
//! Loads YAML contract files and provides name-based field lookup.
//! See docs/evo/003-motion-runtime.md for the design rationale.

mod loader;
mod registry;
mod types;

pub use loader::{load_config, load_contract, StartupConfig};
pub use registry::ContractRegistry;
pub use types::{Contract, FieldDir, FieldSpec, FieldType, PdoMapping, SlaveMatch};
