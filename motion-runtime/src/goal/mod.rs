//! Goal service — 0.8.0 business-level RPC handler core.
//!
//! Each public function corresponds to one gRPC RPC. They take the shared
//! `ContractRegistry` + `PdoBuffers` (the runtime's two state stores) and a
//! plain-data goal struct; they translate goal semantics into RxPDO byte
//! writes and TxPDO byte reads, hiding all field-level concerns from the
//! gRPC layer.
//!
//! The handshake protocol (LS6 only in 0.8.0):
//!
//! - `submit_scara_goal` writes target fields + routine, then flips
//!   `trigger` 0→1 (the **rising edge**). SPEL+ sees the edge and starts
//!   executing the routine.
//! - `read_scara_status` reads the TxPDO status fields. If it observes
//!   `done == true` while `trigger` is still 1, it flips `trigger` 1→0
//!   (the **falling edge**) so SPEL+ unblocks from `Wait Sw(IN_TRIGGER)=0`
//!   and is ready for the next motion. This piggyback is the entire
//!   reason the trigger ever returns to 0 — see EVO-003 "Trigger 边沿协议
//!   在 goal 服务内的实现" for why this design (Option A) instead of a
//!   background poller.
//!
//! Both functions are synchronous and lock the per-slave PDO buffer for
//! the duration of the write/read — that critical section is microseconds.

pub mod scara;

pub use scara::{read_scara_status, submit_scara_goal, ScaraGoalArgs, ScaraStatus};
