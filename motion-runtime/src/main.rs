//! motion-runtime entry point.
//!
//! Boot sequence:
//!   1. Parse CLI args (`--config <path>`).
//!   2. Load startup YAML; load each declared contract into ContractRegistry.
//!   3. Allocate PdoBuffers per declared device (sizes from contract).
//!   4. Spawn gRPC server.
//!   5. Run EtherCAT master loop (stub in 0.7.0 phase 1 — see
//!      docs/evo/003-motion-runtime.md, section on 0.7.0 refactor trade-offs).
//!
//! When the EtherCAT loop is a stub (phase 1), the gRPC server still serves
//! requests; WriteField writes land in the PDO output buffer (so contract +
//! translate logic can be exercised end-to-end) but nothing reaches the bus.
//! Phase 2 (when LS6 hardware is available) replaces ethercat::master::run
//! with the real driver.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Parser;
use tonic::transport::Server;
use tracing::{error, info, warn};

use motion_runtime::contract::{load_config, load_contract, ContractRegistry};
use motion_runtime::ethercat::{self, PdoBuffers};
use motion_runtime::grpc::server::proto::motion_service_server::MotionServiceServer;
use motion_runtime::grpc::server::MotionServiceImpl;

#[derive(Parser, Debug)]
#[command(name = "motion-runtime", version, about)]
struct Cli {
    /// Path to startup config YAML (declares which contracts to load).
    #[arg(short, long)]
    config: PathBuf,

    /// gRPC listen port.
    #[arg(short, long, default_value_t = 50051)]
    port: u16,

    /// EtherCAT NIC name. Currently informational only — IgH reads the NIC
    /// from /etc/sysconfig/ethercat.
    #[arg(short, long, default_value = "eth0")]
    interface: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "motion_runtime=info".into()),
        )
        .init();

    let cli = Cli::parse();
    info!(
        config = %cli.config.display(),
        port = cli.port,
        interface = cli.interface,
        "motion-runtime starting"
    );

    // ── 1. Load startup config + contracts ──────────────────────────────
    let (cfg, base_dir) = load_config(&cli.config)?;
    info!(
        contracts = cfg.contracts.len(),
        "startup config loaded"
    );

    let mut registry = ContractRegistry::new();
    let mut buffers = PdoBuffers::new();

    for rel in &cfg.contracts {
        let abs = base_dir.join(rel);
        let contract = load_contract(&abs)
            .with_context(|| format!("loading contract {}", abs.display()))?;
        info!(
            device = %contract.device,
            description = %contract.description,
            "contract loaded"
        );

        // For phase 1 (no real bus): bind every device to a synthetic slave
        // position by insertion order, so write_field / read_field work
        // end-to-end against the in-memory PDO buffers.
        let slave_position = registry.len() as u16;
        let rx_size = contract.pdo_mapping.rx_pdo_size;
        let tx_size = contract.pdo_mapping.tx_pdo_size;

        let device_name = contract.device.clone();
        registry.insert(contract)?;
        registry.bind_slave(&device_name, slave_position)?;
        buffers.insert(slave_position, rx_size, tx_size);
    }

    let registry = Arc::new(registry);
    let buffers = Arc::new(buffers);

    // ── 2. Start gRPC server ────────────────────────────────────────────
    let grpc_addr: SocketAddr = format!("0.0.0.0:{}", cli.port)
        .parse()
        .context("invalid listen address")?;

    let svc = MotionServiceImpl {
        contracts: Arc::clone(&registry),
        buffers: Arc::clone(&buffers),
    };

    let grpc_handle = tokio::spawn(async move {
        info!(%grpc_addr, "gRPC server listening");
        if let Err(e) = Server::builder()
            .add_service(MotionServiceServer::new(svc))
            .serve(grpc_addr)
            .await
        {
            error!(error = %e, "gRPC server error");
        }
    });

    // ── 3. Run EtherCAT master loop ─────────────────────────────────────
    // Phase 1: this is a stub that returns immediately with an error. We
    // log the warning and keep the gRPC server alive so contract / translate
    // logic can be exercised. Phase 2 will replace it with the real driver
    // and the loop will run forever until ctrl-c.
    match ethercat::master::run(&cli.interface, Arc::clone(&registry), Arc::clone(&buffers)).await {
        Ok(()) => info!("ethercat master loop returned"),
        Err(e) => warn!(
            error = %e,
            "ethercat master not running — gRPC stays online; writes go to buffer but not bus"
        ),
    }

    // Phase 1: keep the process alive so the gRPC server stays up.
    // Phase 2 won't reach here unless the bus loop returns.
    grpc_handle.await.ok();

    Ok(())
}
