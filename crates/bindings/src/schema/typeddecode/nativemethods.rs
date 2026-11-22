//! Object protocol of a schema instance in Rust: `copy`, `deepcopy`, pickling
//! and unpickling.
//!
//! A schema's fields live in slots, so every one of these is "visit the slots
//! by name". The Python versions walk the same names through `copy`, `copyreg`
//! and the generated dataclass methods; here the walk is one loop. Each class
//! gets its own `NativeMethod` per protocol method, built from the slot names
//! of that class, and is installed only when the class does not define the
//! method itself.

use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyString, PyTuple};

use super::assign;

#[derive(Clone, Copy)]
enum Kind {
    Copy,
    DeepCopy,
    Reduce,
    SetState,
}

/// A per-class entry (`validate`, `validate_json`) cached on the class it was
/// built for. Found through another class, as `super().validate` does from an
/// override, it defers to `fallback`, which builds the entry for that class.
#[pyclass(module = "hinoki.core", frozen)]
pub struct Pinned {
    owner: Py<PyAny>,
    entry: Py<PyAny>,
    fallback: Py<PyAny>,
}

#[pymethods]
impl Pinned {
    #[new]
    fn new(owner: Py<PyAny>, entry: Py<PyAny>, fallback: Py<PyAny>) -> Self {
        Self {
            owner,
            entry,
            fallback,
        }
    }

    fn __get__(
        &self,
        py: Python<'_>,
        instance: Option<&Bound<'_, PyAny>>,
        owner: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        match owner {
            Some(found) if !found.is(self.owner.bind(py)) => Ok(self
                .fallback
                .bind(py)
                .call_method1("__get__", (instance, found))?
                .unbind()),
            _ => Ok(self.entry.clone_ref(py)),
        }
    }
}

/// One method of the object protocol for one schema class.
#[pyclass(module = "hinoki.core", frozen)]
pub struct NativeMethod {
    kind: Kind,
    /// Every slot an instance can hold (what the default state holds).
    state: Vec<Py<PyString>>,
}

#[pymethods]
impl NativeMethod {
    #[new]
    fn new(kind: &str, state: Vec<Py<PyString>>) -> PyResult<Self> {
        let kind = match kind {
            "copy" => Kind::Copy,
            "deepcopy" => Kind::DeepCopy,
            "reduce" => Kind::Reduce,
            "setstate" => Kind::SetState,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "unknown method kind {other:?}"
                )))
            }
        };
        Ok(Self { kind, state })
    }

    /// A function-like descriptor: found on an instance it is a method.
    fn __get__<'py>(
        slf: &Bound<'py, Self>,
        instance: &Bound<'py, PyAny>,
        _owner: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if instance.is_none() {
            return Ok(slf.clone().into_any());
        }
        static METHOD_TYPE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
        let py = slf.py();
        let method_type = METHOD_TYPE.get_or_try_init(py, || {
            PyResult::Ok(py.import("types")?.getattr("MethodType")?.unbind())
        })?;
        method_type.bind(py).call1((slf, instance))
    }

    #[pyo3(signature = (instance, argument = None, *_rest))]
    fn __call__<'py>(
        &self,
        instance: &Bound<'py, PyAny>,
        argument: Option<&Bound<'py, PyAny>>,
        _rest: &Bound<'py, PyTuple>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = instance.py();
        match self.kind {
            Kind::Copy => self.copied(instance),
            Kind::DeepCopy => self.deep_copied(instance, argument),
            Kind::Reduce => self.reduced(instance),
            Kind::SetState => {
                Self::restore(instance, argument)?;
                Ok(py.None().into_bound(py))
            }
        }
    }
}

impl NativeMethod {
    /// A new object of the class of `instance` with no slot set.
    fn blank<'py>(instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = instance.py();
        py.get_type::<PyAny>()
            .call_method1("__new__", (instance.get_type(),))
    }

    /// The instance `__dict__`, when it holds anything.
    fn attributes<'py>(instance: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyDict>>> {
        match instance.getattr("__dict__") {
            Ok(found) => {
                let found = found.cast_into::<PyDict>()?;
                Ok((!found.is_empty()).then_some(found))
            }
            Err(error) if error.is_instance_of::<PyAttributeError>(instance.py()) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn copied<'py>(&self, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = instance.py();
        let copy = Self::blank(instance)?;
        if let Some(attributes) = Self::attributes(instance)? {
            for (name, value) in attributes.iter() {
                assign(&copy, name.cast::<PyString>()?, &value)?;
            }
        }
        for name in &self.state {
            let name = name.bind(py);
            match instance.getattr(name) {
                Ok(value) => assign(&copy, name, &value)?,
                Err(error) if error.is_instance_of::<PyAttributeError>(py) => {}
                Err(error) => return Err(error),
            }
        }
        Ok(copy)
    }

    fn deep_copied<'py>(
        &self,
        instance: &Bound<'py, PyAny>,
        memo: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = instance.py();
        static DEEPCOPY: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
        let deepcopy = DEEPCOPY
            .get_or_try_init(py, || {
                PyResult::Ok(py.import("copy")?.getattr("deepcopy")?.unbind())
            })?
            .bind(py);
        let own;
        let memo = match memo {
            Some(memo) => memo,
            None => {
                own = PyDict::new(py).into_any();
                &own
            }
        };
        let copy = Self::blank(instance)?;
        // Registered first so a value that refers back to the instance gets the copy.
        memo.set_item(instance.as_ptr() as usize, &copy)?;
        if let Some(attributes) = Self::attributes(instance)? {
            for (name, value) in attributes.iter() {
                let name = name.cast::<PyString>()?;
                assign(&copy, name, &deepcopy.call1((&value, memo))?)?;
            }
        }
        for name in &self.state {
            let name = name.bind(py);
            let value = match instance.getattr(name) {
                Ok(value) => value,
                Err(error) if error.is_instance_of::<PyAttributeError>(py) => continue,
                Err(error) => return Err(error),
            };
            if atomic(&value) {
                assign(&copy, name, &value)?;
            } else {
                assign(&copy, name, &deepcopy.call1((&value, memo))?)?;
            }
        }
        Ok(copy)
    }

    fn reduced<'py>(&self, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = instance.py();
        static NEWOBJ: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
        let newobj = NEWOBJ
            .get_or_try_init(py, || {
                PyResult::Ok(py.import("copyreg")?.getattr("__newobj__")?.unbind())
            })?
            .bind(py);
        let slots = PyDict::new(py);
        for name in &self.state {
            let name = name.bind(py);
            match instance.getattr(name) {
                Ok(value) => slots.set_item(name, value)?,
                Err(error) if error.is_instance_of::<PyAttributeError>(py) => {}
                Err(error) => return Err(error),
            }
        }
        let attributes = match Self::attributes(instance)? {
            Some(found) => found.copy()?.into_any(),
            None => py.None().into_bound(py),
        };
        let state = PyTuple::new(py, [attributes, slots.into_any()])?;
        let arguments = PyTuple::new(py, [instance.get_type().into_any()])?;
        Ok(PyTuple::new(py, [newobj.clone(), arguments.into_any(), state.into_any()])?.into_any())
    }

    /// `state` is `(attributes, slots)` or a bare dict of attributes.
    fn restore(instance: &Bound<'_, PyAny>, state: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        let Some(state) = state else {
            return Ok(());
        };
        let (attributes, slots) = match state.cast::<PyTuple>() {
            Ok(pair) if pair.len() == 2 => (pair.get_item(0)?, pair.get_item(1)?),
            _ => (state.clone(), state.py().None().into_bound(state.py())),
        };
        for part in [attributes, slots] {
            if part.is_none() {
                continue;
            }
            for (name, value) in part.cast::<PyDict>()?.iter() {
                assign(instance, name.cast::<PyString>()?, &value)?;
            }
        }
        Ok(())
    }
}

/// Values `deepcopy` returns as they are.
fn atomic(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyBytes>()
}
