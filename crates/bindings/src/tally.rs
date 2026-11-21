//! Holds a streamed body to the `content-length` its head declared.
//!
//! Kiso enforces the declared length inside the connection (`Live`). A server
//! that Hinoki does not own cannot do it, so the ASGI pump asks a `Tally` what
//! fits: the length arithmetic is the same pure computation, and it is done
//! here instead of in Python.

use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// What a body of declared `length` has sent so far.
#[derive(Debug, Default)]
struct Count {
    length: Option<u64>,
    total: u64,
    over: bool,
}

impl Count {
    /// How many of the next `size` bytes fit; sets `over` when some do not.
    fn admit(&mut self, size: usize) -> usize {
        let Some(length) = self.length else {
            return size;
        };
        let room = length.saturating_sub(self.total);
        let fits = usize::try_from(room).map_or(size, |room| room.min(size));
        self.total += fits as u64;
        self.over |= fits < size;
        fits
    }

    fn short(&self) -> bool {
        self.length.is_some_and(|length| self.total < length)
    }
}

#[pyclass(name = "Tally", module = "hinoki.core")]
pub struct Tally {
    count: Count,
}

#[pymethods]
impl Tally {
    #[new]
    #[pyo3(signature = (length=None))]
    fn new(length: Option<u64>) -> Self {
        Self {
            count: Count {
                length,
                ..Count::default()
            },
        }
    }

    /// The part of `piece` that fits in the declared length: the piece itself
    /// when all of it does.
    fn fit<'py>(&mut self, piece: &Bound<'py, PyBytes>) -> Bound<'py, PyBytes> {
        let bytes = piece.as_bytes();
        let fits = self.count.admit(bytes.len());
        if fits == bytes.len() {
            piece.clone()
        } else {
            PyBytes::new(piece.py(), &bytes[..fits])
        }
    }

    /// Whether a piece went past the declared length.
    #[getter]
    fn over(&self) -> bool {
        self.count.over
    }

    /// Whether fewer bytes than declared were sent. Always `False` without a length.
    #[getter]
    fn short(&self) -> bool {
        self.count.short()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn count(length: Option<u64>) -> Count {
        Count {
            length,
            ..Count::default()
        }
    }

    #[test]
    fn a_body_without_a_length_is_never_held_back() {
        let mut count = count(None);
        assert_eq!(count.admit(1_000_000), 1_000_000);
        assert!(!count.over && !count.short());
    }

    #[test]
    fn pieces_are_cut_at_the_declared_length() {
        let mut count = count(Some(5));
        assert_eq!(count.admit(3), 3);
        assert_eq!(count.admit(4), 2);
        assert!(count.over);
        assert!(!count.short());
        assert_eq!(count.admit(1), 0);
    }

    #[test]
    fn a_body_that_stops_early_is_short() {
        let mut count = count(Some(5));
        assert_eq!(count.admit(2), 2);
        assert!(count.short() && !count.over);
        assert_eq!(count.admit(3), 3);
        assert!(!count.short());
    }

    #[test]
    fn an_empty_body_with_a_length_of_zero_is_whole() {
        let count = count(Some(0));
        assert!(!count.short() && !count.over);
    }
}
