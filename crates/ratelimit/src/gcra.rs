//! The generic cell rate algorithm on integer microseconds.
//!
//! A bucket is one `i64`, `state = 2 * tat + allowed`: the theoretical arrival
//! time of the next cell and whether the last hit was admitted. One integer is
//! what every SQL backend can hand back from a single statement (MySQL returns
//! only `LAST_INSERT_ID`), so the decision never needs a second read. The SQL in
//! [`crate::sql`] and [`advance`] compute the same step.

/// Longest window: one day.
pub const MAX_WINDOW_US: i64 = 86_400_000_000;
/// Shortest window: one second.
pub const MIN_WINDOW_US: i64 = 1_000_000;
/// Clocks at or above this value are refused, so `2 * (now + window)` cannot overflow.
pub const MAX_NOW_US: i64 = 1 << 60;

const MICROS: i64 = 1_000_000;
const MAX_RETRY_S: i64 = 86_400;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuotaError {
    Limit,
    Window,
    Cost,
    Clock,
}

/// `limit` cells per `window`: one cell every `interval_us`, a burst of `limit`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Quota {
    pub limit: u32,
    pub window_us: i64,
    pub interval_us: i64,
    pub burst_us: i64,
}

/// What one hit adds to a bucket, computed once from the clock and the cost.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Step {
    pub now_us: i64,
    pub inc_us: i64,
    pub burst_us: i64,
    /// The state of a bucket that did not exist: always admitted.
    pub fresh_state: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Allowed,
    Refused,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Decision {
    pub verdict: Verdict,
    pub limit: u32,
    pub remaining: u32,
    pub reset_s: u32,
    pub retry_after_s: u32,
    /// The store was unreachable and the rule says to admit anyway.
    pub failed_open: bool,
}

impl Quota {
    /// `1 <= limit <= window_us` and `1 s <= window <= 1 day`.
    pub fn new(limit: u32, window_us: i64) -> Result<Self, QuotaError> {
        if !(MIN_WINDOW_US..=MAX_WINDOW_US).contains(&window_us) {
            return Err(QuotaError::Window);
        }
        if limit == 0 || i64::from(limit) > window_us {
            return Err(QuotaError::Limit);
        }
        Ok(Self::build(limit, window_us))
    }

    fn build(limit: u32, window_us: i64) -> Self {
        let cells = i64::from(limit);
        let interval_us = (window_us + cells - 1) / cells;
        Self {
            limit,
            window_us,
            interval_us,
            burst_us: interval_us * cells,
        }
    }

    /// The same window with `limit * factor` cells, kept within `window_us`.
    pub fn scaled(self, factor: u32) -> Self {
        let wanted = u64::from(self.limit) * u64::from(factor.max(1));
        let cap = u64::try_from(self.window_us)
            .unwrap_or(0)
            .min(u64::from(u32::MAX));
        let limit = u32::try_from(wanted.min(cap)).unwrap_or(u32::MAX);
        Self::build(limit, self.window_us)
    }

    /// The step of a hit at `now_us` that costs `cost` cells (`1..=limit`).
    pub fn step(&self, now_us: i64, cost: u32) -> Result<Step, QuotaError> {
        if !(0..MAX_NOW_US).contains(&now_us) {
            return Err(QuotaError::Clock);
        }
        if cost == 0 || cost > self.limit {
            return Err(QuotaError::Cost);
        }
        let inc_us = i64::from(cost) * self.interval_us;
        Ok(Step {
            now_us,
            inc_us,
            burst_us: self.burst_us,
            fresh_state: 2 * (now_us + inc_us) + 1,
        })
    }
}

/// The new state of a bucket after one hit: the arithmetic of the SQL statements,
/// on a state held in memory. `None` is a bucket that does not exist yet.
pub fn advance(state: Option<i64>, step: &Step) -> i64 {
    let Some(state) = state else {
        return step.fresh_state;
    };
    let tat = state.max(0) >> 1;
    let next = tat.max(step.now_us) + step.inc_us;
    if next - step.now_us <= step.burst_us {
        2 * next + 1
    } else {
        2 * tat
    }
}

fn seconds_up(micros: i64) -> i64 {
    if micros <= 0 {
        0
    } else {
        (micros + MICROS - 1) / MICROS
    }
}

fn clamp_u32(value: i64) -> u32 {
    u32::try_from(value.max(0)).unwrap_or(u32::MAX)
}

/// Read the state a store returned for `step` as a decision.
pub fn decide(quota: &Quota, step: &Step, state: i64) -> Decision {
    let tat = state.max(0) >> 1;
    if state & 1 == 1 {
        let used = tat - step.now_us;
        return Decision {
            verdict: Verdict::Allowed,
            limit: quota.limit,
            remaining: clamp_u32((step.burst_us - used) / quota.interval_us),
            reset_s: clamp_u32(seconds_up(used)),
            retry_after_s: 0,
            failed_open: false,
        };
    }
    let wait = seconds_up(tat + step.inc_us - step.burst_us - step.now_us);
    Decision {
        verdict: Verdict::Refused,
        limit: quota.limit,
        remaining: 0,
        reset_s: clamp_u32(seconds_up(tat - step.now_us)),
        retry_after_s: clamp_u32(wait.clamp(1, MAX_RETRY_S)),
        failed_open: false,
    }
}

/// A new key refused because the table is full: it may try again in a window.
pub fn refuse_full(quota: &Quota) -> Decision {
    let window = clamp_u32(seconds_up(quota.window_us).clamp(1, MAX_RETRY_S));
    Decision {
        verdict: Verdict::Refused,
        limit: quota.limit,
        remaining: 0,
        reset_s: window,
        retry_after_s: window,
        failed_open: false,
    }
}

/// The store could not be reached and the rule admits anyway.
pub fn fail_open(quota: &Quota) -> Decision {
    Decision {
        verdict: Verdict::Allowed,
        limit: quota.limit,
        remaining: 0,
        reset_s: 0,
        retry_after_s: 0,
        failed_open: true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECOND: i64 = MICROS;

    fn hit(state: &mut Option<i64>, quota: &Quota, now: i64, cost: u32) -> Decision {
        let step = quota.step(now, cost).unwrap();
        let next = advance(*state, &step);
        *state = Some(next);
        decide(quota, &step, next)
    }

    #[test]
    fn a_burst_from_empty_is_exactly_the_limit() {
        let quota = Quota::new(5, 60 * SECOND).unwrap();
        let mut state = None;
        let verdicts: Vec<_> = (0..6)
            .map(|_| hit(&mut state, &quota, 1_000, 1).verdict)
            .collect();
        assert_eq!(verdicts[..5], [Verdict::Allowed; 5]);
        assert_eq!(verdicts[5], Verdict::Refused);
    }

    #[test]
    fn remaining_counts_down_and_retry_after_is_the_wait_for_one_cell() {
        let quota = Quota::new(2, 60 * SECOND).unwrap();
        let mut state = None;
        let first = hit(&mut state, &quota, 0, 1);
        assert_eq!((first.remaining, first.reset_s), (1, 30));
        let second = hit(&mut state, &quota, 0, 1);
        assert_eq!((second.remaining, second.reset_s), (0, 60));
        let refused = hit(&mut state, &quota, 10 * SECOND, 1);
        assert_eq!(refused.verdict, Verdict::Refused);
        assert_eq!(refused.retry_after_s, 20);
        assert_eq!(refused.reset_s, 50);
        let later = hit(&mut state, &quota, 30 * SECOND, 1);
        assert_eq!(later.verdict, Verdict::Allowed);
    }

    #[test]
    fn a_refusal_does_not_consume_quota() {
        let quota = Quota::new(1, 10 * SECOND).unwrap();
        let mut state = None;
        assert_eq!(hit(&mut state, &quota, 0, 1).verdict, Verdict::Allowed);
        for _ in 0..50 {
            assert_eq!(hit(&mut state, &quota, SECOND, 1).verdict, Verdict::Refused);
        }
        assert_eq!(
            hit(&mut state, &quota, 10 * SECOND, 1).verdict,
            Verdict::Allowed
        );
    }

    #[test]
    fn a_costly_hit_takes_several_cells_from_the_same_bucket() {
        let quota = Quota::new(10, 10 * SECOND).unwrap();
        let mut state = None;
        assert_eq!(hit(&mut state, &quota, 0, 7).remaining, 3);
        assert_eq!(hit(&mut state, &quota, 0, 4).verdict, Verdict::Refused);
        assert_eq!(hit(&mut state, &quota, 0, 3).verdict, Verdict::Allowed);
    }

    #[test]
    fn invalid_quotas_steps_and_clocks_are_refused_without_overflow() {
        assert_eq!(Quota::new(0, SECOND), Err(QuotaError::Limit));
        assert_eq!(Quota::new(1, SECOND - 1), Err(QuotaError::Window));
        assert_eq!(Quota::new(1, MAX_WINDOW_US + 1), Err(QuotaError::Window));
        let quota = Quota::new(1, MAX_WINDOW_US).unwrap();
        assert_eq!(quota.step(0, 2), Err(QuotaError::Cost));
        assert_eq!(quota.step(0, 0), Err(QuotaError::Cost));
        assert_eq!(quota.step(-1, 1), Err(QuotaError::Clock));
        assert_eq!(quota.step(MAX_NOW_US, 1), Err(QuotaError::Clock));
        let last = quota.step(MAX_NOW_US - 1, 1).unwrap();
        assert!(last.fresh_state > 0);
        let again = advance(Some(last.fresh_state), &last);
        assert!(again > 0);
    }

    #[test]
    fn a_larger_factor_widens_the_limit_inside_the_window() {
        let quota = Quota::new(10, 60 * SECOND).unwrap();
        assert_eq!(quota.scaled(10).limit, 100);
        assert_eq!(quota.scaled(10).window_us, quota.window_us);
        let tiny = Quota::new(1, SECOND).unwrap();
        assert_eq!(tiny.scaled(u32::MAX).limit, 1_000_000);
    }

    /// Splitmix64: a deterministic generator, so a failure is reproducible.
    fn next(seed: &mut u64) -> u64 {
        *seed = seed.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = *seed;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    #[test]
    fn admitted_cost_in_any_window_never_exceeds_the_burst_plus_the_refill() {
        for case in 0..200u64 {
            let mut seed = case;
            let limit = u32::try_from(1 + next(&mut seed) % 20).unwrap();
            let window = (1 + i64::try_from(next(&mut seed) % 30).unwrap()) * SECOND;
            let quota = Quota::new(limit, window).unwrap();
            let (mut state, mut now) = (None, 0_i64);
            let mut admitted: Vec<(i64, u32)> = Vec::new();
            for _ in 0..400 {
                now += i64::try_from(next(&mut seed) % (window as u64 / 4 + 1)).unwrap();
                let cost = u32::try_from(1 + next(&mut seed) % u64::from(limit.min(3))).unwrap();
                if hit(&mut state, &quota, now, cost).verdict == Verdict::Allowed {
                    admitted.push((now, cost));
                }
            }
            for (start, _) in &admitted {
                let total: u32 = admitted
                    .iter()
                    .filter(|(at, _)| (*start..*start + window).contains(at))
                    .map(|(_, cost)| *cost)
                    .sum();
                assert!(
                    total <= 2 * limit,
                    "case {case}: {total} admitted, limit {limit}"
                );
            }
        }
    }
}
