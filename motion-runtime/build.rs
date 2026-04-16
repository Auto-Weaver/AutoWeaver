fn main() -> Result<(), Box<dyn std::error::Error>> {
    tonic_build::compile_protos("../proto/motion.proto")?;

    // Link IgH EtherCAT userspace library
    println!("cargo:rustc-link-search=native=/usr/local/lib");
    println!("cargo:rustc-link-lib=dylib=ethercat");

    Ok(())
}
