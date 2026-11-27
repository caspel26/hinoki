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
pub mod accesslog;
pub mod body;
pub mod compress;
pub mod conn;
pub mod cors;
pub mod early;
pub mod encode;
pub mod framing;
pub mod headers;
pub mod hosts;
pub mod inputs;
pub mod jobs;
pub mod limits;
pub mod metrics;
pub mod plan;
pub mod proxy;
pub mod queue;
pub mod redirect;
pub mod requestid;
pub mod scrape;
pub mod sse;
pub mod stall;
pub mod stream;
pub mod streams;
pub mod tls;
pub mod ws;
pub mod wsrun;

#[cfg(test)]
mod compress_tests;
#[cfg(test)]
mod jobs_tests;
#[cfg(test)]
mod plan_slot_tests;
#[cfg(test)]
mod policy_tests;

pub use accept::{serve, Closed, ServerConfig, Stop};
pub use accesslog::{AccessLog, Lane, LogFilter, Sink, Trace};
pub use body::{BodyError, BodyReader};
pub use compress::{Compression, Config as CompressionConfig};
pub use conn::{Answer, Backend, Incoming};
pub use early::{Early, EarlyBodies};
pub use encode::{Coding, Levels};
pub use inputs::{convert, Converted, InputValue};
pub use jobs::NativeJobs;
pub use limits::Limits;
pub use metrics::Registry;
pub use plan::{CrudPlanSlot, InputKind, InputParam, Plan, PlanError, RoutePlan};
pub use proxy::Networks;
pub use queue::{InFlight, Permit, Queue};
pub use stream::{
    Aborted, Chunk, Delivery, FastAnswer, FileAnswer, FileBody, Live, Pace, Streamed,
};
