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
