//! Hooks a host can attach to count what the mailer does.

use crate::error::Kind;

/// Called once per send and once per retry, from whatever task ran it. Methods
/// must be cheap and must not block.
pub trait Observer: Send + Sync {
    fn sent(&self, seconds: f64);
    fn failed(&self, kind: Kind, seconds: f64);
    fn retried(&self);
}
