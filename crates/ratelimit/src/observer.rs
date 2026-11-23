//! What a limiter reports to whoever counts its decisions.

/// What one check came to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Allowed,
    Refused,
    /// The store failed and the rule fails closed.
    Unavailable,
    /// The store failed and the rule admitted the request anyway.
    FailedOpen,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Allowed => "allowed",
            Self::Refused => "refused",
            Self::Unavailable => "unavailable",
            Self::FailedOpen => "failed_open",
        }
    }
}

/// Receives one call per check; it must not block or allocate.
pub trait Observer: Send + Sync {
    fn decided(&self, outcome: Outcome);
}
