//! The places a worker has for open streamed responses.
//!
//! `StreamLimit` is the per-worker count behind `execution.max_streams`. A
//! `StreamPermit` is one place: it is given back by `release` or, when the
//! permit is dropped, by the destructor, so a place cannot leak with a lost
//! reference. The count is an atomic, so it needs no lock and holds no Python
//! object.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[pyclass(name = "StreamLimit", module = "hinoki.core", frozen)]
pub struct StreamLimit {
    limit: usize,
    open: Arc<AtomicUsize>,
}

#[pyclass(name = "StreamPermit", module = "hinoki.core", frozen)]
pub struct StreamPermit {
    open: Arc<AtomicUsize>,
    held: AtomicBool,
}

impl StreamPermit {
    fn give_back(&self) {
        if self.held.swap(false, Ordering::AcqRel) {
            self.open.fetch_sub(1, Ordering::AcqRel);
        }
    }
}

impl Drop for StreamPermit {
    fn drop(&mut self) {
        self.give_back();
    }
}

#[pymethods]
impl StreamPermit {
    /// Give the place back. Idempotent.
    fn release(&self) {
        self.give_back();
    }
}

#[pymethods]
impl StreamLimit {
    #[new]
    fn new(limit: usize) -> PyResult<Self> {
        if limit == 0 {
            return Err(PyValueError::new_err("the stream limit must be at least 1"));
        }
        Ok(Self {
            limit,
            open: Arc::new(AtomicUsize::new(0)),
        })
    }

    /// The streams that hold a place now.
    #[getter]
    fn open(&self) -> usize {
        self.open.load(Ordering::Acquire)
    }

    #[getter]
    fn limit(&self) -> usize {
        self.limit
    }

    /// Take a place, or `None` when the worker is full.
    fn acquire(&self) -> Option<StreamPermit> {
        let mut open = self.open.load(Ordering::Acquire);
        loop {
            if open >= self.limit {
                return None;
            }
            match self.open.compare_exchange_weak(
                open,
                open + 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => {
                    return Some(StreamPermit {
                        open: Arc::clone(&self.open),
                        held: AtomicBool::new(true),
                    })
                }
                Err(seen) => open = seen,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_full_limit_refuses_until_a_place_is_given_back() {
        let limit = StreamLimit::new(2).unwrap();
        let first = limit.acquire().unwrap();
        let _second = limit.acquire().unwrap();
        assert!(limit.acquire().is_none());
        first.release();
        first.release();
        assert_eq!(limit.open(), 1);
        assert!(limit.acquire().is_some());
    }

    #[test]
    fn a_dropped_permit_gives_its_place_back() {
        let limit = StreamLimit::new(1).unwrap();
        drop(limit.acquire().unwrap());
        assert_eq!(limit.open(), 0);
        assert!(limit.acquire().is_some());
    }

    #[test]
    fn a_limit_of_zero_is_refused() {
        assert!(StreamLimit::new(0).is_err());
    }
}
