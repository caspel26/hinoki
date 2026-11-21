//! Which file a `File` answer sends.
//!
//! The path is resolved to its real location (symbolic links followed) and
//! must name a regular file; with a `root` it must also lie inside the
//! resolved `root`, so a link or a `..` cannot leave it. The lookup touches
//! the file system, so it runs on a blocking thread and Python awaits it. At
//! most [`MAX_LOOKUPS`] run at once in the process: a lookup that finds the
//! places taken fails at once with `FileLookupBusy` instead of queueing.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use pyo3::prelude::*;

use crate::completion;

pyo3::create_exception!(
    core,
    FileLookupBusy,
    pyo3::exceptions::PyException,
    "Every place for a file lookup is taken."
);

/// Lookups that may run at once, in the whole process.
pub(crate) const MAX_LOOKUPS: usize = 32;

/// A bounded count of running lookups.
#[derive(Debug)]
struct Places {
    limit: usize,
    used: Arc<AtomicUsize>,
}

/// One running lookup; the place is given back when it is dropped.
struct Place(Arc<AtomicUsize>);

impl Drop for Place {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

impl Places {
    fn new(limit: usize) -> Self {
        Self {
            limit,
            used: Arc::new(AtomicUsize::new(0)),
        }
    }

    fn take(&self) -> Option<Place> {
        let mut used = self.used.load(Ordering::Acquire);
        loop {
            if used >= self.limit {
                return None;
            }
            match self.used.compare_exchange_weak(
                used,
                used + 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return Some(Place(Arc::clone(&self.used))),
                Err(seen) => used = seen,
            }
        }
    }
}

fn places() -> &'static Places {
    static PLACES: std::sync::OnceLock<Places> = std::sync::OnceLock::new();
    PLACES.get_or_init(|| Places::new(MAX_LOOKUPS))
}

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

/// Awaitable resolving to `(real_path, size)` or `None`. Raises
/// `FileLookupBusy` at once when `MAX_LOOKUPS` lookups are already running.
#[pyfunction]
#[pyo3(signature = (path, root=None))]
pub(crate) fn locate_file(
    py: Python<'_>,
    path: PathBuf,
    root: Option<PathBuf>,
) -> PyResult<Bound<'_, PyAny>> {
    let Some(place) = places().take() else {
        return Err(FileLookupBusy::new_err("every file lookup place is taken"));
    };
    let find = async move {
        let found = tokio::task::spawn_blocking(move || {
            let _place = place;
            locate(&path, root.as_deref())
        })
        .await
        .map_err(|_| ())?;
        Ok::<_, ()>(found)
    };
    completion::submit_with(
        py,
        find,
        |py, found: Option<(PathBuf, u64)>| match found {
            Some((real, size)) => Ok((real.into_os_string(), size)
                .into_pyobject(py)?
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

    #[cfg(unix)]
    #[test]
    fn a_path_that_is_not_utf8_is_kept_as_it_is() {
        use std::os::unix::ffi::OsStrExt;
        let name = std::ffi::OsStr::from_bytes(b"Cargo.toml\xff");
        // The file system may refuse such a name (APFS does); the lookup must
        // then end as not found, never fail on the encoding.
        let made = std::fs::copy(here().join("Cargo.toml"), here().join(name)).is_ok();
        let found = locate(Path::new(name), Some(&here()));
        if made {
            std::fs::remove_file(here().join(name)).unwrap();
            let (real, _) = found.unwrap();
            assert_eq!(real.file_name(), Some(name));
        } else {
            assert!(found.is_none());
        }
    }

    #[test]
    fn lookups_beyond_the_limit_are_refused_until_one_ends() {
        let places = Places::new(2);
        let first = places.take().unwrap();
        let _second = places.take().unwrap();
        assert!(places.take().is_none());
        drop(first);
        assert!(places.take().is_some());
    }

    #[test]
    fn without_a_root_the_path_stands_alone() {
        assert!(locate(&here().join("Cargo.toml"), None).is_some());
    }
}
