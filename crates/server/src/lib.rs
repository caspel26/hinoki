//! The Hinoki Rust HTTP server (pure Rust, no Python).
//!
//! The parts that need no socket: the per-route [`Plan`], request [`limits`],
//! the [`early`] answers that never reach Python, the request-id rules and the
//! bounded handoff [`queue`]. The parts that do: [`accept`] (listener, connection
//! limit, graceful stop) and [`conn`] (one HTTP/1.1 or HTTP/2 request, the [`Backend`] a
//! caller implements), with the request [`body`] reader and the [`framing`]
//! guard. Nothing here calls into Python; the glue lives in
//! `crates/bindings`.
#![forbid(unsafe_code)]

pub mod accept;
pub mod body;
pub mod conn;
pub mod cors;
pub mod early;
pub mod framing;
pub mod headers;
pub mod hosts;
pub mod limits;
pub mod plan;
pub mod proxy;
pub mod queue;
pub mod redirect;
pub mod requestid;
pub mod stall;
pub mod streams;
pub mod tls;

#[cfg(test)]
mod policy_tests;

pub use accept::{serve, Closed, ServerConfig, Stop};
pub use body::{BodyError, BodyReader};
pub use conn::{Answer, Backend, Incoming};
pub use early::{Early, EarlyBodies};
pub use limits::Limits;
pub use plan::{Plan, PlanError, RoutePlan};
pub use proxy::Networks;
pub use queue::{InFlight, Permit, Queue};
