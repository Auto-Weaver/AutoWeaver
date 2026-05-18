fn main() -> Result<(), Box<dyn std::error::Error>> {
    tonic_build::compile_protos("../proto/motion.proto")?;

    // Link IgH EtherCAT userspace library only when the `igh` feature is on.
    // Default builds (and tests) don't link it, so dev machines without
    // IgH installed can still `cargo build` / `cargo test`.
    //
    // Production builds: `cargo build --release --features igh`
    //
    // Library search path: `/opt/etherlab/lib` is the default install
    // prefix from IgH's `./configure && make install` (without overriding
    // --prefix). If your install uses a different prefix, override via
    // `IGH_LIB_PATH=/your/path cargo build --features igh`.
    if std::env::var("CARGO_FEATURE_IGH").is_ok() {
        let lib_path = std::env::var("IGH_LIB_PATH")
            .unwrap_or_else(|_| "/opt/etherlab/lib".to_string());
        println!("cargo:rustc-link-search=native={}", lib_path);
        println!("cargo:rustc-link-lib=dylib=ethercat");
    }
    println!("cargo:rerun-if-env-changed=CARGO_FEATURE_IGH");
    println!("cargo:rerun-if-env-changed=IGH_LIB_PATH");

    Ok(())
}
