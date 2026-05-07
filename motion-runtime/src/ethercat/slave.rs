/// Information about a single EtherCAT slave discovered during the bus scan.
#[derive(Debug, Clone)]
pub struct SlaveInfo {
    /// Zero-based index on the bus.
    pub index: u16,
    /// Human-readable name (from the slave's EEPROM / SII).
    pub name: String,
    /// EtherCAT vendor ID.
    pub vendor_id: u32,
    /// EtherCAT product code.
    pub product_id: u32,
    /// Whether the slave appears to support CiA402 (heuristic).
    pub has_cia402: bool,
    /// Size of the input PDO (TxPDO) in bytes.
    pub input_pdo_size: usize,
    /// Size of the output PDO (RxPDO) in bytes.
    pub output_pdo_size: usize,
}
