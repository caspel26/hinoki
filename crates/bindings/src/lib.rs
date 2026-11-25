//! Python bindings for the Hinoki native core, exposed as `hinoki.core`.
//!
//! Scope: the path router, the database bridge, the SQL compiler and request
//! decoding / JSON (see `request.rs`).

// Unsafe code is denied crate-wide; each use is allowed on its own function with a SAFETY comment.
#![deny(unsafe_code)]

#[cfg(not(unix))]
compile_error!("Hinoki supports Linux and macOS. On Windows, install and run it inside WSL2.");

mod admintemplate;
mod cache;
mod completion;
mod compression;
mod crudplan;
mod crypto;
mod cursor;
mod db;
mod disposition;
mod driver;
mod filelocate;
mod filters;
mod handoff;
mod jwt;
mod kiso;
mod links;
mod mail;
mod mailtemplate;
mod metrics;
mod migrations;
mod modelrows;
mod naming;
mod nativecrud;
mod nativelinks;
mod nativerun;
mod openapidiff;
mod policy;
mod pytypes;
mod query;
mod ratelimit;
mod recent;
mod request;
mod responsecheck;
mod router;
mod rows;
mod schema;
mod server;
mod sessions;
mod sink;
mod storage;
mod streamlimit;
mod tally;
mod waivers;
mod web;
mod websocket;
mod wsgi;
mod xml;
mod xml_encode;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use router::{Match, RouteError, Router};

/// Requests are built on Tokio threads and released on the event-loop thread;
/// mimalloc frees memory allocated by another thread without contending for
/// the allocating thread's arena.
#[global_allocator]
static ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn to_py_err(err: RouteError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

/// Version of the compiled core (the Cargo package version).
#[pyfunction]
fn core_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Compiled path router. Build it at startup, then treat it as read-only.
#[pyclass(name = "Router", module = "hinoki.core")]
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
fn core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    m.add("CURSOR_MAX_TOKEN", cursor::MAX_TOKEN_CHARS)?;
    m.add_class::<PyRouter>()?;
    m.add_class::<responsecheck::ResponseObjectList>()?;
    m.add_class::<responsecheck::ResponseTypeList>()?;
    m.add_class::<crudplan::PyCrudPlan>()?;
    m.add_class::<filters::PyFilterPlan>()?;
    db::register(m)?;
    server::register(m)?;
    kiso::register(m)?;
    policy::register(m)?;
    mail::register(m)?;
    ratelimit::register(m)?;
    cache::register(m)?;
    mailtemplate::register(m)?;
    wsgi::register(m)?;
    m.add_class::<websocket::WebSocketSpec>()?;
    m.add_class::<websocket::WebSocket>()?;
    m.add_class::<sink::ResponseSink>()?;
    m.add_function(wrap_pyfunction!(filelocate::locate_file, m)?)?;
    m.add(
        "FileLookupBusy",
        m.py().get_type::<filelocate::FileLookupBusy>(),
    )?;
    m.add_function(wrap_pyfunction!(disposition::content_disposition, m)?)?;
    m.add_function(wrap_pyfunction!(disposition::storage_disposition, m)?)?;
    m.add_class::<tally::Tally>()?;
    m.add_class::<streamlimit::StreamLimit>()?;
    m.add_class::<streamlimit::StreamPermit>()?;
    m.add_function(wrap_pyfunction!(sink::sse_frame, m)?)?;
    m.add_function(wrap_pyfunction!(query::compile_query, m)?)?;
    m.add_function(wrap_pyfunction!(links::coerce_link_keys, m)?)?;
    m.add_class::<links::PyLinkSpec>()?;
    m.add_function(wrap_pyfunction!(request::parse_query_string, m)?)?;
    m.add_function(wrap_pyfunction!(xml::parse_xml_body, m)?)?;
    m.add_function(wrap_pyfunction!(xml_encode::encode_xml, m)?)?;
    m.add_function(wrap_pyfunction!(xml_encode::xml_name_ok, m)?)?;
    m.add_function(wrap_pyfunction!(request::parse_cookie_header, m)?)?;
    m.add_function(wrap_pyfunction!(request::header_dict, m)?)?;
    m.add_function(wrap_pyfunction!(request::request_headers, m)?)?;
    m.add_function(wrap_pyfunction!(request::response_headers, m)?)?;
    m.add_function(wrap_pyfunction!(request::parse_multipart_body, m)?)?;
    m.add(
        "MultipartLimitError",
        m.py().get_type::<request::MultipartLimitError>(),
    )?;
    m.add_function(wrap_pyfunction!(request::negotiate_media, m)?)?;
    m.add(
        "JsonDecodeError",
        m.py().get_type::<request::JsonDecodeError>(),
    )?;
    m.add_function(wrap_pyfunction!(request::decode_json, m)?)?;
    m.add_class::<schema::Compiled>()?;
    m.add_class::<schema::NativeInit>()?;
    m.add_class::<schema::NativeMethod>()?;
    m.add_class::<schema::Pinned>()?;
    m.add_function(wrap_pyfunction!(schema::compile_schemas, m)?)?;
    m.add_function(wrap_pyfunction!(schema::check_pattern, m)?)?;
    m.add_function(wrap_pyfunction!(schema::wire_text_of, m)?)?;
    m.add_function(wrap_pyfunction!(schema::dump_instance, m)?)?;
    m.add_function(wrap_pyfunction!(schema::serialize_instance, m)?)?;
    m.add_function(wrap_pyfunction!(schema::dump_for_storage, m)?)?;
    m.add_class::<schema::ModelPlan>()?;
    m.add_class::<rows::PyRowPlan>()?;
    m.add_class::<rows::PyProjectedRows>()?;
    m.add_class::<modelrows::PyModelRows>()?;
    m.add_class::<driver::NativeWait>()?;
    m.add_function(wrap_pyfunction!(driver::_drive, m)?)?;
    m.add_function(wrap_pyfunction!(driver::driving, m)?)?;
    m.add_function(wrap_pyfunction!(driver::_native_wait, m)?)?;
    m.add_class::<nativerun::NativeRun>()?;
    m.add_class::<nativerun::NativeSteps>()?;
    m.add_function(wrap_pyfunction!(nativerun::run_native, m)?)?;
    m.add_function(wrap_pyfunction!(handoff::handoff_batch, m)?)?;
    m.add_function(wrap_pyfunction!(jwt::jwt_encode, m)?)?;
    m.add_function(wrap_pyfunction!(jwt::jwt_decode, m)?)?;
    m.add_class::<jwt::PyJWTVerifier>()?;
    m.add_class::<jwt::PyClaimsAuth>()?;
    m.add_function(wrap_pyfunction!(crypto::hash_password, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::verify_password, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::password_needs_rehash, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::make_token, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::make_uuid7, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::constant_time_equal, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::sha256_hex, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::hmac_sha256_hex, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::hmac_sha256_verify, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::csrf_mac, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::csrf_mint, m)?)?;
    m.add_function(wrap_pyfunction!(crypto::csrf_verify, m)?)?;
    m.add_function(wrap_pyfunction!(web::encode_header, m)?)?;
    m.add_function(wrap_pyfunction!(web::checked_headers, m)?)?;
    m.add_function(wrap_pyfunction!(web::set_cookie_header, m)?)?;
    m.add_function(wrap_pyfunction!(web::decode_path_params, m)?)?;
    m.add_function(wrap_pyfunction!(web::parse_bool, m)?)?;
    m.add_function(wrap_pyfunction!(web::parse_int, m)?)?;
    m.add_function(wrap_pyfunction!(web::parse_float, m)?)?;
    m.add_function(wrap_pyfunction!(web::parse_uuid, m)?)?;
    m.add_function(wrap_pyfunction!(web::extract_boundary, m)?)?;
    m.add_function(wrap_pyfunction!(web::same_site_origin, m)?)?;
    m.add_function(wrap_pyfunction!(web::py_websocket_origin_allowed, m)?)?;
    m.add_function(wrap_pyfunction!(web::parse_page_params, m)?)?;
    m.add_function(wrap_pyfunction!(web::parse_cursor_params, m)?)?;
    m.add_function(wrap_pyfunction!(web::parse_dotenv, m)?)?;
    m.add_function(wrap_pyfunction!(web::is_host_pattern, m)?)?;
    m.add_function(wrap_pyfunction!(web::is_valid_origin, m)?)?;
    m.add_function(wrap_pyfunction!(web::is_http_token_str, m)?)?;
    m.add_function(wrap_pyfunction!(web::is_media_rule, m)?)?;
    m.add_function(wrap_pyfunction!(naming::snake_case, m)?)?;
    m.add_function(wrap_pyfunction!(naming::app_label, m)?)?;
    m.add_function(wrap_pyfunction!(naming::resolve_model_names, m)?)?;
    m.add_function(wrap_pyfunction!(storage::validate_storage_key, m)?)?;
    m.add_function(wrap_pyfunction!(storage::normalize_content_type, m)?)?;
    m.add_function(wrap_pyfunction!(storage::new_storage_key, m)?)?;
    m.add_class::<storage::NativeMemoryStorage>()?;
    m.add_function(wrap_pyfunction!(admintemplate::html_escape, m)?)?;
    m.add_function(wrap_pyfunction!(admintemplate::template_placeholders, m)?)?;
    m.add_class::<admintemplate::AdminTemplate>()?;
    m.add_function(wrap_pyfunction!(recent::append_recent, m)?)?;
    m.add_function(wrap_pyfunction!(recent::read_recent, m)?)?;
    m.add_function(wrap_pyfunction!(cache::validate_cache_key, m)?)?;
    m.add_function(wrap_pyfunction!(cache::validate_cache_ttl, m)?)?;
    m.add_class::<sessions::NativeMemorySessionStore>()?;
    m.add_function(wrap_pyfunction!(waivers::fnmatch_case, m)?)?;
    m.add_function(wrap_pyfunction!(waivers::validate_waiver, m)?)?;
    m.add_function(wrap_pyfunction!(openapidiff::openapi_breaking_changes, m)?)?;
    migrations::register(m)?;
    m.add_function(wrap_pyfunction!(request::encode_json, m)?)?;
    Ok(())
}
