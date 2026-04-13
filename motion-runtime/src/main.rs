mod cia402;
mod device;
mod ethercat;
mod grpc;

use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Parser;
use tokio::sync::Mutex;
use tonic::transport::Server;
use tracing::{error, info};

use crate::device::manager::DeviceManager;
use crate::grpc::server::proto::motion_service_server::MotionServiceServer;
use crate::grpc::server::MotionServiceImpl;

/// AutoWeaver motion-runtime: EtherCAT real-time motion controller with gRPC interface.
#[derive(Parser, Debug)]
#[command(name = "motion-runtime", version, about)]
struct Cli {
    /// Network interface for EtherCAT (e.g. "eth0").
    #[arg(short, long, default_value = "eth0")]
    interface: String,

    /// gRPC listen port.
    #[arg(short, long, default_value_t = 50051)]
    port: u16,
}

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize structured logging.
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "motion_runtime=info".into()),
        )
        .init();

    let cli = Cli::parse();
    info!(
        interface = cli.interface,
        port = cli.port,
        "Starting motion-runtime"
    );

    // Shared device manager — accessed by both gRPC and cyclic loop.
    let manager = Arc::new(Mutex::new(DeviceManager::new()));

    // --- Start gRPC server ---
    let grpc_addr: SocketAddr = format!("0.0.0.0:{}", cli.port)
        .parse()
        .context("Invalid listen address")?;

    let svc = MotionServiceImpl {
        manager: Arc::clone(&manager),
    };

    tokio::spawn(async move {
        info!(%grpc_addr, "gRPC server listening");
        if let Err(e) = Server::builder()
            .add_service(MotionServiceServer::new(svc))
            .serve(grpc_addr)
            .await
        {
            error!("gRPC server error: {}", e);
        }
    });

    // --- EtherCAT: init, scan, register devices, then run cyclic loop ---
    // This call blocks forever (or until a fatal error).
    crate::ethercat::master::run(&cli.interface, Arc::clone(&manager)).await?;

    Ok(())
}
