//! Python bindings of the rate limiter: `RateLimiter` (database or memory),
//! `RateRule`, `RateDecision` and `RateLimitMetrics`. Only plain values cross;
//! the counting runs on the shared tokio runtime, in one SQL statement per hit.

use std::sync::Arc;
use std::time::Duration;

use hinoki_ratelimit::gcra::{Decision, Quota, QuotaError, Verdict};
use hinoki_ratelimit::key::{KeyError, KeyParts, Who};
use hinoki_ratelimit::observer::{Observer, Outcome};
use hinoki_ratelimit::store::{LimitError, Limiter, Limits, Rule, MAX_TIMEOUT};
use hinoki_ratelimit::{system_now_us, TableName};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

use crate::completion;
use crate::db::PyDatabase;

pyo3::create_exception!(core, RateLimitUnavailable, PyRuntimeError);

const WHO_CLIENT: u8 = 0;
const WHO_CALLER: u8 = 1;
const WHO_CUSTOM: u8 = 2;
const MICROS: i64 = 1_000_000;

fn invalid(message: &str) -> PyErr {
    PyValueError::new_err(format!("rate limit: {message}"))
}

fn limit_error(error: LimitError) -> PyErr {
    match error {
        LimitError::Unavailable { schema: true } => {
            RateLimitUnavailable::new_err("rate limit table is missing or out of date")
        }
        LimitError::Unavailable { schema: false } => {
            RateLimitUnavailable::new_err("rate limit storage unavailable")
        }
        LimitError::Key(KeyError::TooLong) => invalid("a key part is longer than 1024 bytes"),
        LimitError::Key(KeyError::InvalidTable) => invalid("invalid table name"),
        LimitError::Quota(QuotaError::Cost) => invalid("cost must be between 1 and the limit"),
        LimitError::Quota(QuotaError::Clock) => invalid("clock out of range"),
        LimitError::Quota(_) => invalid("invalid limit or window"),
    }
}

/// A limit, its window, the cost of a hit and what to do when storage is down,
/// validated once.
#[pyclass(name = "RateRule", module = "hinoki.core", frozen)]
pub(crate) struct PyRateRule {
    rule: Rule,
}

#[pymethods]
impl PyRateRule {
    #[new]
    fn new(
        limit: u32,
        window_s: u32,
        cost: u32,
        unknown_factor: u32,
        fail_open: bool,
    ) -> PyResult<Self> {
        let window = i64::from(window_s) * MICROS;
        let quota =
            Quota::new(limit, window).map_err(|error| limit_error(LimitError::Quota(error)))?;
        if cost == 0 || cost > limit {
            return Err(limit_error(LimitError::Quota(QuotaError::Cost)));
        }
        Ok(Self {
            rule: Rule {
                quota,
                unknown: quota.scaled(unknown_factor),
                cost,
                fail_open,
            },
        })
    }
}

/// The outcome of one check.
#[pyclass(name = "RateDecision", module = "hinoki.core", frozen, get_all)]
pub(crate) struct PyDecision {
    allowed: bool,
    limit: u32,
    remaining: u32,
    reset: u32,
    retry_after: u32,
    failed_open: bool,
}

impl From<Decision> for PyDecision {
    fn from(decision: Decision) -> Self {
        Self {
            allowed: decision.verdict == Verdict::Allowed,
            limit: decision.limit,
            remaining: decision.remaining,
            reset: decision.reset_s,
            retry_after: decision.retry_after_s,
            failed_open: decision.failed_open,
        }
    }
}

struct OwnedKey {
    scope: String,
    who: u8,
    ident: Option<String>,
    tenant: Option<String>,
}

impl OwnedKey {
    fn new(route: &str, who: u8, ident: Option<&str>, tenant: Option<&str>) -> PyResult<Self> {
        match (who, ident) {
            (WHO_CLIENT, _) | (WHO_CALLER | WHO_CUSTOM, Some(_)) => {}
            (WHO_CALLER | WHO_CUSTOM, None) => return Err(invalid("a caller or key is required")),
            _ => return Err(invalid("unknown key kind")),
        }
        Ok(Self {
            scope: route.to_owned(),
            who,
            ident: ident.map(str::to_owned),
            tenant: tenant.map(str::to_owned),
        })
    }

    fn parts(&self) -> KeyParts<'_> {
        let ident = self.ident.as_deref();
        let who = match self.who {
            WHO_CLIENT => Who::Client(ident),
            WHO_CALLER => Who::Caller(ident.unwrap_or_default()),
            _ => Who::Custom(ident.unwrap_or_default()),
        };
        KeyParts {
            scope: &self.scope,
            who,
            tenant: self.tenant.as_deref(),
        }
    }
}

struct Recorder {
    registry: Arc<kiso::Registry>,
}

impl Observer for Recorder {
    fn decided(&self, outcome: Outcome) {
        self.registry.observe_rate_limit(outcome.as_str());
    }
}

/// Where a limiter records its decisions: the fixed rate limit series of the
/// server's metrics registry.
#[pyclass(name = "RateLimitMetrics", module = "hinoki.core", frozen)]
pub(crate) struct RateLimitMetrics {
    registry: Arc<kiso::Registry>,
}

impl RateLimitMetrics {
    pub(crate) fn new(registry: Arc<kiso::Registry>) -> Self {
        Self { registry }
    }
}

#[pymethods]
impl RateLimitMetrics {
    /// Count a decision (`allowed`, `refused`, `unavailable` or `failed_open`)
    /// of a policy that is not the built-in limiter.
    fn record(&self, decision: &str) {
        self.registry.observe_rate_limit(decision);
    }
}

fn limits_of(max_rows: u64, timeout_ms: u64, sweep_every: u32) -> PyResult<Limits> {
    let timeout = Duration::from_millis(timeout_ms);
    if timeout.is_zero() || timeout > MAX_TIMEOUT {
        return Err(invalid("timeout must be between 1 ms and 5 s"));
    }
    if max_rows == 0 || sweep_every == 0 {
        return Err(invalid("max_rows and sweep_every must be at least 1"));
    }
    Ok(Limits {
        max_rows,
        timeout,
        sweep_every,
    })
}

/// Refuse a blocking check where blocking would stall a loop or the runtime.
fn refuse_blocking(py: Python<'_>) -> PyResult<()> {
    let on_loop = py
        .import("asyncio")?
        .call_method0("get_running_loop")
        .is_ok();
    if on_loop || crate::driver::driving() || tokio::runtime::Handle::try_current().is_ok() {
        return Err(PyRuntimeError::new_err(
            "rate limit: a blocking check cannot run on an event loop or runtime thread",
        ));
    }
    Ok(())
}

/// The limiter of one worker: a table shared by every worker, or process memory.
/// Create it in the lifespan or on first use, never at import.
#[pyclass(name = "RateLimiter", module = "hinoki.core", frozen)]
pub(crate) struct PyRateLimiter {
    inner: Arc<Limiter>,
}

#[pymethods]
impl PyRateLimiter {
    #[staticmethod]
    fn database(
        db: &PyDatabase,
        table: &str,
        max_rows: u64,
        timeout_ms: u64,
        sweep_every: u32,
    ) -> PyResult<Self> {
        let table = TableName::new(table).map_err(|error| limit_error(LimitError::Key(error)))?;
        let limits = limits_of(max_rows, timeout_ms, sweep_every)?;
        Ok(Self {
            inner: Arc::new(Limiter::database(db.clone_inner(), &table, limits)),
        })
    }

    #[staticmethod]
    fn memory(max_rows: u64, sweep_every: u32) -> PyResult<Self> {
        let limits = limits_of(max_rows, 1, sweep_every)?;
        Ok(Self {
            inner: Arc::new(Limiter::memory(limits)),
        })
    }

    /// Count one hit. Awaitable; `target` is `(who, ident)`: `who` is 0 (client
    /// address, `ident` may be `None`), 1 (authenticated caller) or 2 (custom
    /// key). `now_us` defaults to the wall clock.
    #[pyo3(signature = (rule, route, target, tenant=None, now_us=None))]
    fn check<'py>(
        &self,
        py: Python<'py>,
        rule: &PyRateRule,
        route: &str,
        target: (u8, Option<String>),
        tenant: Option<&str>,
        now_us: Option<i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        crate::db::ensure_runtime();
        let key = OwnedKey::new(route, target.0, target.1.as_deref(), tenant)?;
        let (limiter, rule) = (Arc::clone(&self.inner), rule.rule);
        let now = now_us.unwrap_or_else(system_now_us);
        let work = async move { limiter.check(&rule, &key.parts(), now).await };
        let convert = |py: Python<'_>, decision: Decision| {
            Ok(Bound::new(py, PyDecision::from(decision))?
                .into_any()
                .unbind())
        };
        completion::submit_with(
            py,
            work,
            convert,
            (
                |_: Python<'_>, error| limit_error(error),
                |_: Python<'_>| RateLimitUnavailable::new_err("rate limit storage unavailable"),
            ),
        )
    }

    /// [`check`](Self::check) for a thread that is not an event loop: releases
    /// the GIL and waits. Raises `RuntimeError` on a loop or runtime thread.
    #[pyo3(signature = (rule, route, target, tenant=None, now_us=None))]
    fn check_blocking(
        &self,
        py: Python<'_>,
        rule: &PyRateRule,
        route: &str,
        target: (u8, Option<String>),
        tenant: Option<&str>,
        now_us: Option<i64>,
    ) -> PyResult<PyDecision> {
        refuse_blocking(py)?;
        crate::db::ensure_runtime();
        let key = OwnedKey::new(route, target.0, target.1.as_deref(), tenant)?;
        let (limiter, rule) = (Arc::clone(&self.inner), rule.rule);
        let now = now_us.unwrap_or_else(system_now_us);
        let runtime = pyo3_async_runtimes::tokio::get_runtime();
        py.detach(|| runtime.block_on(async move { limiter.check(&rule, &key.parts(), now).await }))
            .map(PyDecision::from)
            .map_err(limit_error)
    }

    /// Record decisions into `metrics` from now on, replacing earlier metrics.
    fn attach_metrics(&self, metrics: &RateLimitMetrics) {
        self.inner.attach(Arc::new(Recorder {
            registry: Arc::clone(&metrics.registry),
        }));
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyRateLimiter>()?;
    m.add_class::<PyRateRule>()?;
    m.add_class::<PyDecision>()?;
    m.add_class::<RateLimitMetrics>()?;
    m.add(
        "RateLimitUnavailable",
        m.py().get_type::<RateLimitUnavailable>(),
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_rule_refuses_a_cost_above_the_limit_and_a_window_out_of_range() {
        assert!(PyRateRule::new(5, 60, 5, 10, false).is_ok());
        assert!(PyRateRule::new(5, 60, 6, 10, false).is_err());
        assert!(PyRateRule::new(5, 0, 1, 10, false).is_err());
        assert!(PyRateRule::new(0, 60, 1, 10, false).is_err());
        assert!(PyRateRule::new(1, 86_401, 1, 10, false).is_err());
    }

    #[test]
    fn limits_are_bounded() {
        assert!(limits_of(10, 500, 100).is_ok());
        assert!(limits_of(10, 0, 100).is_err());
        assert!(limits_of(10, 5_001, 100).is_err());
        assert!(limits_of(0, 500, 100).is_err());
        assert!(limits_of(10, 500, 0).is_err());
    }
}
