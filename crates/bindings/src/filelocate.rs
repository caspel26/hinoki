//! Which file a `File` answer sends.
//!
//! The path is resolved to its real location (symbolic links followed) and
//! must name a regular file; with a `root` it must also lie inside the
//! resolved `root`, so a link or a `..` cannot leave it. The lookup touches
//! the file system, so it runs on a blocking thread and Python awaits it.

use std::path::{Path, PathBuf};

use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::completion;

/// The real path and the size of the regular file `path` names, or `None`
/// when there is none or it is outside `root`. A relative `path` is taken
/// from `root`.
pub(crate) fn locate(path: &Path, root: Option<&Path>) -> Option<(PathBuf, u64)> {
    let real = match root {
        Some(root) => {
            let base = root.canonicalize().ok()?;
            let real = base.join(path).canonicalize().ok()?;
            real.starts_with(&base).then_some(real)?
        }
        None => path.canonicalize().ok()?,
    };
    let meta = real.metadata().ok()?;
    meta.is_file().then_some((real, meta.len()))
}

/// Awaitable resolving to `(real_path, size)` or `None`.
#[pyfunction]
#[pyo3(signature = (path, root=None))]
pub(crate) fn locate_file(
    py: Python<'_>,
    path: PathBuf,
    root: Option<PathBuf>,
) -> PyResult<Bound<'_, PyAny>> {
    let find = async move {
        let found = tokio::task::spawn_blocking(move || locate(&path, root.as_deref()))
            .await
            .map_err(|_| ())?;
        Ok::<_, ()>(found.and_then(|(real, size)| Some((real.to_str()?.to_owned(), size))))
    };
    completion::submit_with(
        py,
        find,
        |py, found: Option<(String, u64)>| match found {
            Some(location) => Ok(PyTuple::new(
                py,
                [
                    location.0.into_pyobject(py)?.into_any(),
                    location.1.into_pyobject(py)?.into_any(),
                ],
            )?
            .into_any()
            .unbind()),
            None => Ok(py.None()),
        },
        (
            |_: Python<'_>, (): ()| {
                pyo3::exceptions::PyRuntimeError::new_err("the file lookup failed")
            },
            |_: Python<'_>| {
                pyo3::exceptions::PyRuntimeError::new_err("the file lookup task failed")
            },
        ),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn here() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
    }

    #[test]
    fn a_regular_file_is_found_with_its_real_path_and_size() {
        let (real, size) = locate(Path::new("Cargo.toml"), Some(&here())).unwrap();
        assert_eq!(real, here().canonicalize().unwrap().join("Cargo.toml"));
        assert_eq!(size, std::fs::metadata(real).unwrap().len());
    }

    #[test]
    fn a_path_outside_the_root_is_not_found() {
        assert!(locate(Path::new("../kiso/Cargo.toml"), Some(&here())).is_none());
        let outside = here().join("../kiso/Cargo.toml");
        assert!(locate(&outside, Some(&here())).is_none());
    }

    #[test]
    fn a_directory_or_a_missing_file_is_not_found() {
        assert!(locate(Path::new("src"), Some(&here())).is_none());
        assert!(locate(Path::new("nothing.here"), Some(&here())).is_none());
        assert!(locate(Path::new("a\0b"), None).is_none());
    }

    #[test]
    fn without_a_root_the_path_stands_alone() {
        assert!(locate(&here().join("Cargo.toml"), None).is_some());
    }
}
