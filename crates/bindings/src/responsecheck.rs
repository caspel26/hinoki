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
