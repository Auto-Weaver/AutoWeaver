//! Simple gRPC client to test motion-runtime.
//!
//! Usage:
//!   cargo build --release --example move_test
//!   ./target/release/examples/move_test [--target 10000] [--velocity 5000] [--addr 127.0.0.1:50051]

use std::time::Duration;

pub mod proto {
    tonic::include_proto!("motion");
}

use proto::motion_service_client::MotionServiceClient;
use proto::{FeedbackRequest, GoalRequest, ResultRequest};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let addr = std::env::args()
        .position(|a| a == "--addr")
        .and_then(|i| std::env::args().nth(i + 1))
        .unwrap_or_else(|| "http://127.0.0.1:50051".into());

    let target: i32 = std::env::args()
        .position(|a| a == "--target")
        .and_then(|i| std::env::args().nth(i + 1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(10000);

    let velocity: u32 = std::env::args()
        .position(|a| a == "--velocity")
        .and_then(|i| std::env::args().nth(i + 1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(5000);

    println!("Connecting to {}...", addr);
    let mut client = MotionServiceClient::connect(addr).await?;

    // 1. Check current feedback
    println!("\n--- Current feedback ---");
    let fb = client
        .get_feedback(FeedbackRequest { axis_id: 0 })
        .await?
        .into_inner();
    println!(
        "  position={} state={} progress={}%",
        fb.current_position, fb.state, fb.progress_pct
    );

    // 2. Send goal
    println!("\n--- Sending goal: target={} velocity={} ---", target, velocity);
    let resp = client
        .send_goal(GoalRequest {
            axis_id: 0,
            target_position: target,
            velocity,
            timeout_secs: 10.0,
        })
        .await?
        .into_inner();
    println!("  accepted={} message={}", resp.accepted, resp.message);

    if !resp.accepted {
        eprintln!("Goal rejected, exiting.");
        return Ok(());
    }

    // 3. Poll feedback until done
    println!("\n--- Polling feedback ---");
    loop {
        tokio::time::sleep(Duration::from_millis(200)).await;

        let fb = client
            .get_feedback(FeedbackRequest { axis_id: 0 })
            .await?
            .into_inner();
        print!(
            "\r  pos={:<10} state={:<25} progress={:.1}%   ",
            fb.current_position, fb.state, fb.progress_pct
        );

        // Check for result
        if let Ok(res) = client
            .get_result(ResultRequest { axis_id: 0 })
            .await
        {
            let r = res.into_inner();
            println!("\n\n--- Result ---");
            println!(
                "  success={} final_pos={} error_code={} error_msg={}",
                r.success, r.final_position, r.error_code, r.error_message
            );
            break;
        }
    }

    Ok(())
}
