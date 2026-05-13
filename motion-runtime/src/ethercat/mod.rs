//! EtherCAT bus handling.
//!
//! - `pdo_buffers`: shared RxPDO/TxPDO byte buffers between gRPC and the
//!   cyclic loop. Stable, well-tested.
//! - `slave`: scanned-slave metadata type.
//! - `master`: stub entry point for the cyclic loop. Real implementation
//!   lands when LS6 hardware is available.
//! - `igh_ffi`: thin FFI bindings to libethercat. Compiled only with the
//!   `igh` feature on (so dev machines without IgH can still build/test).

#[cfg(feature = "igh")]
pub mod igh_ffi;

pub mod master;
pub mod pdo_buffers;
pub mod slave;

pub use pdo_buffers::PdoBuffers;
