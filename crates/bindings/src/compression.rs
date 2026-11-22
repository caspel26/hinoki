//! Reads the `compression` settings Python passes to a server into a Kiso
//! compression policy. The dict is copied here; no Python object is kept.

use std::sync::Arc;

use kiso::{Coding, Compression, CompressionConfig};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

pub(crate) fn field<'py, T: FromPyObjectOwned<'py>>(
    options: &Bound<'py, PyDict>,
    name: &str,
) -> PyResult<Option<T>> {
    options
        .get_item(name)?
        .map(|value| value.extract().map_err(Into::into))
        .transpose()
}

fn coding(name: &str) -> PyResult<Coding> {
    match name {
        "br" => Ok(Coding::Brotli),
        "gzip" => Ok(Coding::Gzip),
        other => Err(PyValueError::new_err(format!(
            "compression: unknown encoding {other:?} (use \"br\" or \"gzip\")"
        ))),
    }
}

/// The policy for `options`, or `None` when compression is absent or disabled.
pub(crate) fn parse(options: Option<&Bound<'_, PyDict>>) -> PyResult<Option<Arc<Compression>>> {
    let Some(options) = options else {
        return Ok(None);
    };
    let mut config = CompressionConfig::default();
    if let Some(names) = field::<Vec<String>>(options, "encodings")? {
        config.codings = names
            .iter()
            .map(|name| coding(name))
            .collect::<PyResult<_>>()?;
    }
    if let Some(types) = field::<Vec<String>>(options, "types")? {
        config.types = types;
    }
    if let Some(value) = field(options, "min_size")? {
        config.min_size = value;
    }
    if let Some(value) = field(options, "event_streams")? {
        config.event_streams = value;
    }
    if let Some(value) = field(options, "files")? {
        config.files = value;
    }
    if let Some(value) = field(options, "gzip_level")? {
        config.levels.gzip = value;
    }
    if let Some(value) = field(options, "brotli_quality")? {
        config.levels.brotli_quality = value;
    }
    if let Some(value) = field(options, "brotli_window")? {
        config.levels.brotli_window = value;
    }
    if let Some(value) = field(options, "max_concurrent")? {
        config.max_concurrent = value;
    }
    Compression::new(config)
        .map(|policy| Some(Arc::new(policy)))
        .map_err(PyValueError::new_err)
}
