//! The Hinoki Rust HTTP server (pure Rust, no Python).
//!
//! The parts that need no socket: the per-route [`Plan`], request [`limits`],
//! the [`early`] answers that never reach Python, the request-id rules and the
//! bounded handoff [`queue`]. The parts that do: [`accept`] (listener, connection
//! limit, graceful stop) and [`conn`] (one HTTP/1.1 request, the [`Backend`] a
//! caller implements). Nothing here calls into Python; the glue lives in
//! `crates/bindings`.
#![forbid(unsafe_code)]

pub mod accept;
pub mod conn;
pub mod cors;
pub mod early;
pub mod headers;
pub mod limits;
pub mod plan;
pub mod queue;
pub mod requestid;

pub use accept::{serve, Closed, ServerConfig, Stop};
pub use conn::{Answer, Backend, Incoming};
pub use early::{Early, EarlyBodies};
pub use limits::Limits;
pub use plan::{Plan, PlanError, RoutePlan};
pub use queue::{InFlight, Permit, Queue};
