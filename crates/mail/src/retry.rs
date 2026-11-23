//! Which failures are tried again, and how long to wait before the next try.

use std::time::Duration;

use crate::error::{ConfigError, Kind, MailError};

pub const MAX_RETRIES: u32 = 5;
const MAX_BACKOFF_EXPONENT: u32 = 16;

/// Extra attempts after the first one, with exponential backoff and jitter.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RetryPolicy {
    pub retries: u32,
    pub base: Duration,
    pub max: Duration,
}

impl RetryPolicy {
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.retries > MAX_RETRIES {
            return Err(ConfigError("retries must be at most 5"));
        }
        if self.base.is_zero() || self.max < self.base {
            return Err(ConfigError(
                "backoff_base must be positive and at most backoff_max",
            ));
        }
        Ok(())
    }

    /// The wait before attempt `attempt` (2 for the first retry): the base
    /// doubled for each earlier retry, capped at the maximum, then scaled by
    /// `0.5 + 0.5 * jitter` for a `jitter` in `0.0..=1.0`.
    pub fn backoff(&self, attempt: u32, jitter: f64) -> Duration {
        let exponent = attempt.saturating_sub(2).min(MAX_BACKOFF_EXPONENT);
        let wait = self.base.saturating_mul(1 << exponent).min(self.max);
        wait.mul_f64(0.5 + 0.5 * jitter.clamp(0.0, 1.0))
    }

    /// Whether to try again after `error` as attempt number `attempts`.
    pub fn should_retry(&self, error: &MailError, attempts: u32) -> bool {
        attempts <= self.retries && retryable(error)
    }
}

/// A failure that may go away: the connection or the server was not usable.
/// Nothing is retried once the end of the message was written without an
/// answer (the message may have been accepted), and a refusal is final.
pub(crate) fn retryable(error: &MailError) -> bool {
    !error.possibly_sent
        && matches!(
            error.kind,
            Kind::Connect | Kind::Timeout | Kind::Temporary | Kind::Protocol
        )
}

/// A random number in `0.0..=1.0`; `1.0` when no randomness is available.
pub(crate) fn jitter() -> f64 {
    let mut bytes = [0u8; 4];
    match getrandom::fill(&mut bytes) {
        Ok(()) => f64::from(u32::from_le_bytes(bytes)) / f64::from(u32::MAX),
        Err(_) => 1.0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy() -> RetryPolicy {
        RetryPolicy {
            retries: 2,
            base: Duration::from_millis(100),
            max: Duration::from_millis(350),
        }
    }

    #[test]
    fn backoff_doubles_is_capped_and_scaled_by_jitter() {
        let policy = policy();
        assert_eq!(policy.backoff(2, 1.0), Duration::from_millis(100));
        assert_eq!(policy.backoff(3, 1.0), Duration::from_millis(200));
        assert_eq!(policy.backoff(4, 1.0), Duration::from_millis(350));
        assert_eq!(policy.backoff(40, 1.0), Duration::from_millis(350));
        assert_eq!(policy.backoff(2, 0.0), Duration::from_millis(50));
        assert_eq!(policy.backoff(3, 0.5), Duration::from_millis(150));
        assert_eq!(policy.backoff(2, 7.0), Duration::from_millis(100));
        assert_eq!(policy.backoff(0, -3.0), Duration::from_millis(50));
    }

    #[test]
    fn only_transient_failures_are_retried_within_the_budget() {
        let policy = policy();
        for kind in Kind::ALL {
            let retried = policy.should_retry(&MailError::new(kind), 1);
            let expected = matches!(
                kind,
                Kind::Connect | Kind::Timeout | Kind::Temporary | Kind::Protocol
            );
            assert_eq!(retried, expected, "{kind:?}");
        }
        let temporary = MailError::new(Kind::Temporary);
        assert!(policy.should_retry(&temporary, 2));
        assert!(!policy.should_retry(&temporary, 3));
    }

    #[test]
    fn a_message_that_may_have_been_sent_is_never_retried() {
        let mut error = MailError::new(Kind::Timeout);
        error.possibly_sent = true;
        assert!(!policy().should_retry(&error, 1));
    }

    #[test]
    fn invalid_policies_are_refused() {
        let base = policy();
        assert_eq!(base.validate(), Ok(()));
        assert!(RetryPolicy { retries: 6, ..base }.validate().is_err());
        assert!(RetryPolicy {
            base: Duration::ZERO,
            ..base
        }
        .validate()
        .is_err());
        assert!(RetryPolicy {
            max: Duration::from_millis(99),
            ..base
        }
        .validate()
        .is_err());
        assert_eq!(RetryPolicy { retries: 5, ..base }.validate(), Ok(()));
    }

    #[test]
    fn jitter_is_a_fraction() {
        for _ in 0..100 {
            assert!((0.0..=1.0).contains(&jitter()));
        }
    }
}
