//! Python bindings for the Hinoki native core, exposed as `hinoki.native`.
//!
//! Scope: the path router, the database bridge, the SQL compiler and request
//! decoding / JSON (see `request.rs`).

mod db;
mod query;
mod request;
mod router;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use router::{Match, RouteError, Router};

fn to_py_err(err: RouteError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

/// Version of the compiled core (the Cargo package version).
#[pyfunction]
fn core_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Compiled path router. Build it at startup, then treat it as read-only.
#[pyclass(name = "Router", module = "hinoki.native")]
struct PyRouter {
    inner: Router,
}

#[pymethods]
impl PyRouter {
    #[new]
    fn new() -> Self {
        Self {
            inner: Router::new(),
        }
    }

    /// Register `action` for `method` and `template`; raises `ValueError`
    /// for malformed templates, duplicate routes or parameter-name conflicts.
    fn add(&mut self, method: &str, template: &str, action: u32) -> PyResult<()> {
        self.inner.add(method, template, action).map_err(to_py_err)
    }

    /// Return `(action, params)`, `("method_not_allowed", allowed_methods)`
    /// or `None` when the path is unknown.
    fn find<'py>(
        &self,
        py: Python<'py>,
        method: &str,
        path: &str,
    ) -> PyResult<Option<(Bound<'py, PyAny>, Bound<'py, PyAny>)>> {
        Ok(match self.inner.find(method, path) {
            Match::NotFound => None,
            Match::MethodNotAllowed { allowed } => Some((
                "method_not_allowed".into_pyobject(py)?.into_any(),
                allowed.into_pyobject(py)?.into_any(),
            )),
            Match::Found { action, params } => {
                let dict = PyDict::new(py);
                for (k, v) in params {
                    dict.set_item(k, v)?;
                }
                Some((action.into_pyobject(py)?.into_any(), dict.into_any()))
            }
        })
    }
}

#[pymodule]
fn native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    m.add_class::<PyRouter>()?;
    db::register(m)?;
    m.add_function(wrap_pyfunction!(query::compile_query, m)?)?;
    m.add_function(wrap_pyfunction!(request::parse_query_string, m)?)?;
    m.add_function(wrap_pyfunction!(request::parse_cookie_header, m)?)?;
    m.add_function(wrap_pyfunction!(request::header_dict, m)?)?;
    m.add_function(wrap_pyfunction!(request::parse_multipart_body, m)?)?;
    m.add_function(wrap_pyfunction!(request::decode_json, m)?)?;
    m.add_function(wrap_pyfunction!(request::encode_json, m)?)?;
    Ok(())
}
