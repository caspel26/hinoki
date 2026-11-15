//! Building blocks of the Hinoki Rust HTTP server (pure Rust, no Python).
//!
//! This crate holds the parts that need no socket and no interpreter: the
//! per-route [`Plan`], request [`limits`], the [`early`] answers that never reach
//! Python, the request-id rules and the bounded handoff [`queue`]. Nothing here
//! calls into Python; the glue lives in `crates/bindings`.
#![forbid(unsafe_code)]

pub mod early;
pub mod limits;
pub mod plan;
pub mod queue;
pub mod requestid;

pub use early::{Early, EarlyBodies};
pub use limits::Limits;
pub use plan::{Plan, PlanError, RoutePlan};
pub use queue::{InFlight, Permit, Queue};
