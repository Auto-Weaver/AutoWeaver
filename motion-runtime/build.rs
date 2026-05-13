fn main() -> Result<(), Box<dyn std::error::Error>> {
    tonic_build::compile_protos("../proto/motion.proto")?;

    // Link IgH EtherCAT userspace library only when the `igh` feature is on.
    // Default builds (and tests) don't link it, so dev machines without
    // IgH installed can still `cargo build` / `cargo test`.
    //
    // Production builds: `cargo build --release --features igh`
    if std::env::var("CARGO_FEATURE_IGH").is_ok() {
        println!("cargo:rustc-link-search=native=/usr/local/lib");
        println!("cargo:rustc-link-lib=dylib=ethercat");
    }
    println!("cargo:rerun-if-env-changed=CARGO_FEATURE_IGH");

    Ok(())
}
