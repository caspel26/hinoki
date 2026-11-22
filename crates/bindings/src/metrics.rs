//! Reads the `metrics` settings Python passes to a server: the registry the
//! transport records into and the dedicated listener that serves it. The dict
//! is copied here; no Python object is kept.

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use kiso::metrics::{Config, DEFAULT_BUCKETS};
use kiso::scrape::{publish, serve_metrics, Access, Endpoint, Shared};
use kiso::{Closed, Networks, Registry};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use tokio::net::TcpListener;
use tokio::sync::watch;

use crate::compression::field;
use crate::kiso::{inherited_listener, io_error};

/// Everything a server needs to record and serve metrics.
pub(crate) struct Metrics {
    pub(crate) registry: Arc<Registry>,
    listener: std::net::TcpListener,
    endpoint: Arc<Endpoint>,
    shared: Option<Shared>,
}

fn required<'py, T: FromPyObjectOwned<'py>>(
    options: &Bound<'py, PyDict>,
    name: &str,
) -> PyResult<T> {
    field(options, name)?
        .ok_or_else(|| PyValueError::new_err(format!("metrics: {name} is required")))
}

fn listener_of(options: &Bound<'_, PyDict>) -> PyResult<std::net::TcpListener> {
    let listener = match field::<i32>(options, "fd")? {
        Some(fd) => inherited_listener(fd)?,
        None => {
            let host: String = required(options, "host")?;
            let port: u16 = required(options, "port")?;
            std::net::TcpListener::bind((host.as_str(), port)).map_err(io_error)?
        }
    };
    listener.set_nonblocking(true).map_err(io_error)?;
    Ok(listener)
}

fn shared_of(options: &Bound<'_, PyDict>, worker: u32) -> PyResult<Option<Shared>> {
    let Some(dir) = field::<String>(options, "dir")? else {
        return Ok(None);
    };
    let interval: f64 = required(options, "snapshot_interval")?;
    let interval = Duration::try_from_secs_f64(interval)
        .ok()
        .filter(|wait| !wait.is_zero())
        .ok_or_else(|| PyValueError::new_err("metrics: snapshot_interval must be > 0"))?;
    Ok(Some(Shared {
        dir: dir.into(),
        slot: worker,
        interval,
    }))
}

fn access_of(options: &Bound<'_, PyDict>) -> PyResult<Access> {
    let allow: Vec<String> = field(options, "allow")?.unwrap_or_default();
    let allow = if allow.is_empty() {
        None
    } else {
        Some(Networks::parse(&allow).map_err(PyValueError::new_err)?)
    };
    Ok(Access {
        path: required(options, "path")?,
        allow,
        token: field(options, "token")?,
    })
}

/// The metrics for `options`, or `None` when they are absent. `routes` are the
/// action numbers and patterns of the application.
pub(crate) fn parse(
    options: Option<&Bound<'_, PyDict>>,
    routes: &[(u32, Arc<str>)],
) -> PyResult<Option<Metrics>> {
    let Some(options) = options else {
        return Ok(None);
    };
    let worker: u32 = field(options, "worker")?.unwrap_or(0);
    let config = Config {
        worker,
        version: field(options, "version")?.unwrap_or_default(),
        buckets: field(options, "buckets")?.unwrap_or_else(|| DEFAULT_BUCKETS.to_vec()),
    };
    let registry = Registry::new(config, routes)
        .map(Arc::new)
        .map_err(|error| PyValueError::new_err(format!("metrics: {error}")))?;
    let shared = shared_of(options, worker)?;
    let access = access_of(options)?;
    let listener = listener_of(options)?;
    let endpoint = Arc::new(Endpoint::new(Arc::clone(&registry), access, shared.clone()));
    Ok(Some(Metrics {
        registry,
        listener,
        endpoint,
        shared,
    }))
}

impl Metrics {
    /// Serve scrapes and write this worker's snapshot until `stop`.
    async fn run(self, stop: watch::Receiver<bool>) {
        let Ok(listener) = TcpListener::from_std(self.listener) else {
            return;
        };
        let snapshots = async {
            if let Some(shared) = self.shared {
                publish(Arc::clone(&self.registry), shared, stop.clone()).await;
            }
        };
        tokio::join!(
            serve_metrics(listener, self.endpoint, stop.clone()),
            snapshots
        );
    }
}

/// Run `main` and, with metrics, the metrics listener beside it; ends when both
/// have. The metrics are stopped by the same `stop` as the server.
pub(crate) async fn alongside(
    main: impl Future<Output = Closed>,
    metrics: Option<Metrics>,
    stop: watch::Receiver<bool>,
) -> Closed {
    match metrics {
        None => main.await,
        Some(metrics) => tokio::join!(main, metrics.run(stop)).0,
    }
}
