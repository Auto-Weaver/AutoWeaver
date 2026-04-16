//! Minimal EtherCAT bus scan using IgH master.
//! Usage: cargo build --release --example scan && sudo ./target/release/examples/scan
//!
//! No arguments needed — IgH uses the NIC configured in /etc/sysconfig/ethercat.

use motion_runtime::ethercat::igh_ffi::*;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("Requesting IgH EtherCAT master 0...");

    let master = unsafe { ecrt_request_master(0) };
    if master.is_null() {
        eprintln!("Failed to request master — is the ethercat service running?");
        std::process::exit(1);
    }

    let mut info: ec_master_info_t = unsafe { std::mem::zeroed() };
    let ret = unsafe { ecrt_master(master, &mut info) };
    if ret != 0 {
        eprintln!("ecrt_master() failed: {}", ret);
        unsafe { ecrt_release_master(master) };
        std::process::exit(1);
    }

    println!("Found {} slave(s):", info.slave_count);

    for pos in 0..info.slave_count as u16 {
        let mut si: ec_slave_info_t = unsafe { std::mem::zeroed() };
        let ret = unsafe { ecrt_master_get_slave(master, pos, &mut si) };
        if ret != 0 {
            println!("  [{}] <failed to read>", pos);
            continue;
        }
        let name = {
            let len = si.name.iter().position(|&b| b == 0).unwrap_or(si.name.len());
            String::from_utf8_lossy(&si.name[..len]).to_string()
        };
        println!(
            "  [{}] name={:?} vendor=0x{:08X} product=0x{:08X} rev=0x{:08X}",
            pos, name, si.vendor_id, si.product_code, si.revision_number,
        );
    }

    unsafe { ecrt_release_master(master) };
    println!("Master released.");
    Ok(())
}
