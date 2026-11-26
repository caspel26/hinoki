use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};

/// Fast check for list[dict[str, object]], with Python checks for subclasses.
#[pyclass(name = "_ResponseObjectList", module = "hinoki.core")]
pub(crate) struct ResponseObjectList {
    fallback: Py<PyAny>,
    item: Py<PyAny>,
}

#[pymethods]
impl ResponseObjectList {
    #[new]
    fn new(fallback: Py<PyAny>, item: Py<PyAny>) -> Self {
        Self { fallback, item }
    }

    fn __call__(&self, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = value.py();
        if !value.is_exact_instance_of::<PyList>() {
            return self.fallback.bind(py).call1((value,))?.is_truthy();
        }
        let list = value.cast::<PyList>()?;
        let mut index = 0;
        while index < list.len() {
            let entry = list.get_item(index)?;
            let valid = if entry.is_exact_instance_of::<PyDict>() {
                entry
                    .cast::<PyDict>()?
                    .iter()
                    .all(|(key, _)| key.is_exact_instance_of::<PyString>())
            } else {
                self.item.bind(py).call1((&entry,))?.is_truthy()?
            };
            if !valid {
                return Ok(false);
            }
            index += 1;
        }
        Ok(true)
    }
}

/// Fast check for list[Schema] and list[Model], with Python checks for list subclasses.
#[pyclass(module = "hinoki.core", frozen, name = "_ResponseTypeList")]
pub struct ResponseTypeList {
    target: Py<PyAny>,
    fallback: Py<PyAny>,
}

#[pymethods]
impl ResponseTypeList {
    #[new]
    fn new(target: Py<PyAny>, fallback: Py<PyAny>) -> Self {
        Self { target, fallback }
    }

    fn __call__(&self, py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        if !value.is_exact_instance_of::<PyList>() {
            return self.fallback.bind(py).call1((value,))?.is_truthy();
        }
        let list = value.cast::<PyList>()?;
        let target = self.target.bind(py);
        let mut index = 0;
        while index < list.len() {
            let item = list.get_item(index)?;
            if !item.is_instance(target)? {
                return Ok(false);
            }
            index += 1;
        }
        Ok(true)
    }

    /// The check and the narrowing scan in one pass: `0` invalid, `1` valid with
    /// every item exactly the target, `2` valid with a subclass item or a value
    /// whose items are not scanned (a list subclass).
    fn scan(&self, py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<u8> {
        if !value.is_exact_instance_of::<PyList>() {
            let valid = self.fallback.bind(py).call1((value,))?.is_truthy()?;
            return Ok(if valid { 2 } else { 0 });
        }
        let list = value.cast::<PyList>()?;
        let target = self.target.bind(py);
        let exact_type = target.as_ptr();
        let mut exact = true;
        let mut index = 0;
        while index < list.len() {
            let item = list.get_item(index)?;
            if item.get_type_ptr().cast::<ffi::PyObject>() != exact_type {
                if !item.is_instance(target)? {
                    return Ok(0);
                }
                exact = false;
            }
            index += 1;
        }
        Ok(if exact { 1 } else { 2 })
    }
}

/// Whether `value` is an exact `list` (not a subclass, whose items may be lazy)
/// whose every item is exactly of the type `target`, so a response needs no
/// narrowing to the declared Schema.
#[pyfunction]
pub(crate) fn all_exact(value: &Bound<'_, PyAny>, target: &Bound<'_, PyAny>) -> bool {
    let target = target.as_ptr();
    value.cast_exact::<PyList>().is_ok_and(|list| {
        list.iter()
            .all(|item| item.get_type_ptr().cast::<ffi::PyObject>() == target)
    })
}
