//! Thin FFI bindings to the IgH EtherCAT Master userspace library (ecrt.h).
//!
//! Only the subset of functions and types needed by motion-runtime is declared
//! here.  Link with `-lethercat` (provided by IgH `make install`).

#![allow(non_camel_case_types, dead_code)]

use std::os::raw::{c_int, c_uint, c_void};

// ---------------------------------------------------------------------------
// Opaque handle types
// ---------------------------------------------------------------------------

pub enum ec_master {}
pub enum ec_domain {}
pub enum ec_slave_config {}

pub type ec_master_t = ec_master;
pub type ec_domain_t = ec_domain;
pub type ec_slave_config_t = ec_slave_config;

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

#[repr(C)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ec_direction_t {
    EC_DIR_INVALID = 0,
    EC_DIR_OUTPUT = 1,
    EC_DIR_INPUT = 2,
    EC_DIR_COUNT = 3,
}

#[repr(C)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ec_watchdog_mode_t {
    EC_WD_DEFAULT = 0,
    EC_WD_ENABLE = 1,
    EC_WD_DISABLE = 2,
}

// ---------------------------------------------------------------------------
// Structs for PDO configuration
// ---------------------------------------------------------------------------

/// PDO entry description (index + subindex + bit_length).
#[repr(C)]
pub struct ec_pdo_entry_info_t {
    pub index: u16,
    pub subindex: u8,
    pub bit_length: u8,
}

/// PDO description (index + number of entries + pointer to entries).
#[repr(C)]
pub struct ec_pdo_info_t {
    pub index: u16,
    pub n_entries: c_uint,
    pub entries: *mut ec_pdo_entry_info_t,
}

/// Sync manager description.
#[repr(C)]
pub struct ec_sync_info_t {
    pub index: u8,
    pub dir: ec_direction_t,
    pub n_pdos: c_uint,
    pub pdos: *mut ec_pdo_info_t,
    pub watchdog_mode: ec_watchdog_mode_t,
}

/// PDO entry registration record (for mass-registration).
#[repr(C)]
pub struct ec_pdo_entry_reg_t {
    pub alias: u16,
    pub position: u16,
    pub vendor_id: u32,
    pub product_code: u32,
    pub index: u16,
    pub subindex: u8,
    pub offset: *mut c_uint,
    pub bit_position: *mut c_uint,
}

// ---------------------------------------------------------------------------
// State structs
// ---------------------------------------------------------------------------

#[repr(C)]
#[derive(Debug, Default)]
pub struct ec_master_state_t {
    pub slaves_responding: c_uint,
    /// Bitfield: al_states (4 bits) + link_up (1 bit).
    /// We store as a single u32 and extract with methods.
    _bitfield: u32,
}

impl ec_master_state_t {
    pub fn al_states(&self) -> u32 {
        self._bitfield & 0x0F
    }
    pub fn link_up(&self) -> bool {
        (self._bitfield >> 4) & 1 != 0
    }
}

#[repr(C)]
#[derive(Debug, Default)]
pub struct ec_domain_state_t {
    pub working_counter: c_uint,
    pub wc_state: c_uint,
    pub redundancy_active: c_uint,
}

#[repr(C)]
#[derive(Debug, Default)]
pub struct ec_slave_config_state_t {
    /// Bitfield: online (1) + operational (1) + al_state (4).
    _bitfield: u32,
}

impl ec_slave_config_state_t {
    pub fn online(&self) -> bool {
        self._bitfield & 1 != 0
    }
    pub fn operational(&self) -> bool {
        (self._bitfield >> 1) & 1 != 0
    }
    pub fn al_state(&self) -> u32 {
        (self._bitfield >> 2) & 0x0F
    }
}

/// Layout verified: total 16 bytes.
///   offset 0: slave_count (u32)
///   offset 4: link_up:1 + scan_busy packed in u32
///   offset 8: app_time (u64)
#[repr(C)]
pub struct ec_master_info_t {
    pub slave_count: c_uint,
    _bitfield_and_scan: u32,  // link_up:1 bit, then scan_busy at byte offset 5
    pub app_time: u64,
}

/// Layout verified with offsetof() on target:
///   total size = 176, name at offset 110.
#[repr(C)]
pub struct ec_slave_info_t {
    pub position: u16,          // offset 0
    _pad0: u16,                 // padding to align vendor_id
    pub vendor_id: u32,         // offset 4
    pub product_code: u32,      // offset 8
    pub revision_number: u32,   // offset 12
    pub serial_number: u32,     // offset 16
    pub alias: u16,             // offset 20
    pub current_on_ebus: i16,   // offset 22
    _ports: [u8; 80],           // offset 24: ports[4] struct array
    pub al_state: u8,           // offset 104
    pub error_flag: u8,         // offset 105
    pub sync_count: u8,         // offset 106
    _pad1: u8,                  // padding
    pub sdo_count: u16,         // offset 108
    pub name: [u8; 64],         // offset 110
    _pad2: [u8; 2],             // pad to 176
}

// ---------------------------------------------------------------------------
// Sentinel value for end-of-list markers
// ---------------------------------------------------------------------------

/// Marks the end of a sync_info array (index = 0xFF).
pub const EC_END: u8 = 0xFF;

// ---------------------------------------------------------------------------
// extern "C" function declarations
// ---------------------------------------------------------------------------

#[link(name = "ethercat")]
extern "C" {
    // -- Master lifecycle --
    pub fn ecrt_request_master(master_index: c_uint) -> *mut ec_master_t;
    pub fn ecrt_release_master(master: *mut ec_master_t);

    // -- Master info --
    pub fn ecrt_master(
        master: *mut ec_master_t,
        master_info: *mut ec_master_info_t,
    ) -> c_int;
    pub fn ecrt_master_get_slave(
        master: *mut ec_master_t,
        position: u16,
        slave_info: *mut ec_slave_info_t,
    ) -> c_int;

    // -- Domain --
    pub fn ecrt_master_create_domain(master: *mut ec_master_t) -> *mut ec_domain_t;
    pub fn ecrt_domain_reg_pdo_entry_list(
        domain: *mut ec_domain_t,
        regs: *const ec_pdo_entry_reg_t,
    ) -> c_int;
    pub fn ecrt_domain_size(domain: *const ec_domain_t) -> usize;
    pub fn ecrt_domain_data(domain: *mut ec_domain_t) -> *mut u8;
    pub fn ecrt_domain_process(domain: *mut ec_domain_t);
    pub fn ecrt_domain_queue(domain: *mut ec_domain_t);
    pub fn ecrt_domain_state(domain: *const ec_domain_t, state: *mut ec_domain_state_t);

    // -- Slave configuration --
    pub fn ecrt_master_slave_config(
        master: *mut ec_master_t,
        alias: u16,
        position: u16,
        vendor_id: u32,
        product_code: u32,
    ) -> *mut ec_slave_config_t;

    pub fn ecrt_slave_config_pdos(
        sc: *mut ec_slave_config_t,
        n_syncs: c_uint,
        syncs: *const ec_sync_info_t,
    ) -> c_int;

    pub fn ecrt_slave_config_reg_pdo_entry(
        sc: *mut ec_slave_config_t,
        entry_index: u16,
        entry_subindex: u8,
        domain: *mut ec_domain_t,
        bit_position: *mut c_uint,
    ) -> c_int;

    pub fn ecrt_slave_config_dc(
        sc: *mut ec_slave_config_t,
        assign_activate: u16,
        sync0_cycle: u32,
        sync0_shift: i32,
        sync1_cycle: u32,
        sync1_shift: i32,
    );

    pub fn ecrt_slave_config_state(
        sc: *const ec_slave_config_t,
        state: *mut ec_slave_config_state_t,
    );

    /// Add an SDO configuration (32-bit) to be written during slave configuration.
    /// This is applied before the master transitions to OP.
    pub fn ecrt_slave_config_sdo32(
        sc: *mut ec_slave_config_t,
        index: u16,
        subindex: u8,
        value: u32,
    ) -> c_int;

    /// Add an SDO configuration (16-bit).
    pub fn ecrt_slave_config_sdo16(
        sc: *mut ec_slave_config_t,
        index: u16,
        subindex: u8,
        value: u16,
    ) -> c_int;

    /// Add an SDO configuration (8-bit).
    pub fn ecrt_slave_config_sdo8(
        sc: *mut ec_slave_config_t,
        index: u16,
        subindex: u8,
        value: u8,
    ) -> c_int;

    // -- Master activation & cyclic --
    pub fn ecrt_master_activate(master: *mut ec_master_t) -> c_int;
    pub fn ecrt_master_deactivate(master: *mut ec_master_t);
    pub fn ecrt_master_set_send_interval(
        master: *mut ec_master_t,
        send_interval: usize,
    ) -> c_int;

    pub fn ecrt_master_send(master: *mut ec_master_t);
    pub fn ecrt_master_receive(master: *mut ec_master_t);
    pub fn ecrt_master_state(master: *const ec_master_t, state: *mut ec_master_state_t);

    // -- Distributed clocks --
    pub fn ecrt_master_application_time(master: *mut ec_master_t, app_time: u64);
    pub fn ecrt_master_sync_reference_clock(master: *mut ec_master_t);
    pub fn ecrt_master_sync_slave_clocks(master: *mut ec_master_t);
    pub fn ecrt_master_sync_reference_clock_to(
        master: *mut ec_master_t,
        ref_time: u64,
    );
}
