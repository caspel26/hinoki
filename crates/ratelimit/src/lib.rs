//! Hinoki rate limiting (pure Rust): the GCRA step, key normalisation and the
//! stores that keep the state of a bucket, in one SQL statement per hit or in
//! memory. The Python bridge lives in `hinoki-bindings`.

#![forbid(unsafe_code)]

pub mod gcra;
pub mod key;
pub mod observer;
mod sql;
pub mod store;

pub use sql::TableName;

use std::time::{SystemTime, UNIX_EPOCH};

/// The wall clock in microseconds since the Unix epoch, as the buckets count time.
pub fn system_now_us() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| i64::try_from(elapsed.as_micros()).unwrap_or(0))
}
