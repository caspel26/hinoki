//! The native constructor: `Item(name="x", price=1.0)` without Python frames.
//!
//! A schema class built from its own generated constructor runs, on every
//! call, that constructor, the acceptance check and the post-construction
//! steps, all in Python. When the arguments are keywords whose values the
//! compiled plan vouches for (exactly the declared types, inside every bound),
//! this callable validates them in Rust and writes them straight into the
//! slots of the instance. It never decides that something is invalid: any
//! value, name or shape it cannot vouch for runs the original constructor with
//! the original arguments, which reports what it always reported.
//!
//! A default factory is called before the values are known to be accepted, so
//! when a value is then refused (and the original constructor runs) the
//! factory is called a second time.

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PyString, PyTuple};

use super::{store, within, Bounds, Plan, PlanClass, PlanField, Shape, Typed};
use crate::schema::{Compiled, Number};

/// Static nesting beyond which nothing is vouched for.
const MAX_DEPTH: usize = 64;
/// Integers up to this magnitude are exact `f64`s.
const EXACT_INT: i64 = 1 << 53;

/// What a field takes when its keyword is absent.
pub(super) enum Omitted {
    /// Nothing is known: the original constructor decides (a required field,
    /// or a default the plan cannot vouch for).
    Unknown,
    Value(Py<PyAny>),
    Factory(Py<PyAny>),
}

/// The plan, once the class could be resolved.
struct Ready {
    typed: Arc<Typed>,
}

/// The constructor of one schema class: native when it can be, else `slow`.
#[pyclass(module = "hinoki.core", frozen)]
pub struct NativeInit {
    /// The constructor that ran before: it handles every other case.
    slow: Py<PyAny>,
    /// `() -> CompiledSchema | None`, called until it answers.
    prepare: Py<PyAny>,
    ready: PyOnceLock<Option<Ready>>,
}

#[pymethods]
impl NativeInit {
    #[new]
    fn new(slow: Py<PyAny>, prepare: Py<PyAny>) -> Self {
        Self {
            slow,
            prepare,
            ready: PyOnceLock::new(),
        }
    }

    #[getter(__wrapped__)]
    fn wrapped(&self, py: Python<'_>) -> Py<PyAny> {
        self.slow.clone_ref(py)
    }

    fn __getattr__(&self, py: Python<'_>, name: &Bound<'_, PyString>) -> PyResult<Py<PyAny>> {
        Ok(self.slow.bind(py).getattr(name)?.unbind())
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

    #[pyo3(signature = (instance, *args, **kwargs))]
    fn __call__(
        &self,
        py: Python<'_>,
        instance: &Bound<'_, PyAny>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        if args.is_empty() {
            if let Some(Some(ready)) = self.resolve(py) {
                if build(py, ready, instance, kwargs)? {
                    return Ok(());
                }
            }
        }
        let mut all = Vec::with_capacity(args.len() + 1);
        all.push(instance.clone());
        all.extend(args.iter());
        self.slow.bind(py).call(PyTuple::new(py, all)?, kwargs)?;
        Ok(())
    }
}

impl NativeInit {
    /// The resolved plan (`Some(None)`: this class has none), or `None` when
    /// the class cannot be resolved yet: the original constructor then runs
    /// and reports its own error, and the next call tries again.
    fn resolve(&self, py: Python<'_>) -> Option<&Option<Ready>> {
        if let Some(found) = self.ready.get(py) {
            return Some(found);
        }
        let ready = prepared(py, self.prepare.bind(py)).ok()?;
        Some(self.ready.get_or_init(py, || ready))
    }
}

fn prepared(_py: Python<'_>, prepare: &Bound<'_, PyAny>) -> PyResult<Option<Ready>> {
    let found = prepare.call0()?;
    if found.is_none() {
        return Ok(None);
    }
    let compiled: Bound<'_, Compiled> = found.extract()?;
    let Some(typed) = compiled.borrow().typed.clone() else {
        return Ok(None);
    };
    if typed.classes[typed.root].layout.is_none() {
        return Ok(None);
    }
    Ok(Some(Ready { typed }))
}

/// What every omitted field of `class` takes, from the `defaults` and the
/// default `factories` of its dataclass fields.
pub(super) fn omitted_of(
    typed: &Typed,
    class: &PlanClass,
    values: &Bound<'_, PyDict>,
    factories: &Bound<'_, PyDict>,
) -> PyResult<Vec<Omitted>> {
    let py = values.py();
    let mut omitted = Vec::with_capacity(class.fields.len());
    for field in &class.fields {
        let name = field.name.bind(py);
        let found = if let Some(value) = values.get_item(name)? {
            match accept(typed, &field.plan, &value, 0) {
                Some(value) => Omitted::Value(value.unbind()),
                None => Omitted::Unknown,
            }
        } else if let Some(factory) = factories.get_item(name)? {
            Omitted::Factory(factory.unbind())
        } else {
            Omitted::Unknown
        };
        omitted.push(found);
    }
    Ok(omitted)
}

/// The supplied-fields value of a record the validator completed.
pub(crate) struct Completed<'py> {
    pub(crate) supplied: Option<Bound<'py, PyAny>>,
}

/// Give the fields `record` lacks their defaults, when every one has a default
/// the plan vouches for; `None` leaves `record` as it was.
pub(crate) fn complete<'py>(
    typed: &Typed,
    index: usize,
    record: &Bound<'py, PyDict>,
) -> PyResult<Option<Completed<'py>>> {
    let py = record.py();
    let class = &typed.classes[index];
    if class.omitted.len() != class.fields.len() {
        return Ok(None);
    }
    let mut found = Vec::new();
    for (field, default) in class.fields.iter().zip(&class.omitted) {
        let name = field.name.bind(py);
        if record.contains(name)? {
            continue;
        }
        match default_of(py, typed, field, default)? {
            Some(value) => found.push((name, value)),
            None => return Ok(None),
        }
    }
    let supplied = match &class.supplied {
        Some(_) => Some(PyFrozenSet::new(py, record.keys())?.into_any()),
        None => None,
    };
    for (name, value) in found {
        record.set_item(name, value)?;
    }
    Ok(Some(Completed { supplied }))
}

fn default_of<'py>(
    py: Python<'py>,
    typed: &Typed,
    field: &PlanField,
    default: &Omitted,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    Ok(match default {
        Omitted::Unknown => None,
        Omitted::Value(held) => Some(held.bind(py).clone()),
        Omitted::Factory(factory) => accept(typed, &field.plan, &factory.bind(py).call0()?, 0),
    })
}

/// Validate and write the keywords; `false` means "run the original".
fn build<'py>(
    py: Python<'py>,
    ready: &Ready,
    instance: &Bound<'py, PyAny>,
    kwargs: Option<&Bound<'py, PyDict>>,
) -> PyResult<bool> {
    let typed = &*ready.typed;
    let class = &typed.classes[typed.root];
    if !instance.get_type().is(class.class.bind(py)) {
        return Ok(false);
    }
    let given = kwargs.map_or(0, |found| found.len());
    let mut values: Vec<Option<Bound<'py, PyAny>>> = Vec::with_capacity(class.fields.len());
    let mut matched = 0;
    for field in &class.fields {
        let value = match kwargs {
            Some(found) => found.get_item(field.name.bind(py))?,
            None => None,
        };
        let Some(value) = value else {
            values.push(None);
            continue;
        };
        let Some(accepted) = accept(typed, &field.plan, &value, 0) else {
            return Ok(false);
        };
        matched += 1;
        values.push(Some(accepted));
    }
    if matched != given {
        return Ok(false);
    }
    if matched < class.fields.len() && !fill_omitted(py, typed, class, &mut values)? {
        return Ok(false);
    }
    let partial = matched < class.fields.len();
    write(py, class, instance, values, partial, || match kwargs {
        Some(found) => Ok(PyFrozenSet::new(py, found.keys())?.into_any()),
        None => Ok(PyFrozenSet::empty(py)?.into_any()),
    })?;
    Ok(true)
}

/// Give the absent fields their defaults; `false` when one has none the plan
/// can vouch for.
fn fill_omitted<'py>(
    py: Python<'py>,
    typed: &Typed,
    class: &PlanClass,
    values: &mut [Option<Bound<'py, PyAny>>],
) -> PyResult<bool> {
    if class.omitted.len() != class.fields.len() {
        return Ok(false);
    }
    for ((value, default), field) in values.iter_mut().zip(&class.omitted).zip(&class.fields) {
        if value.is_some() {
            continue;
        }
        match default_of(py, typed, field, default)? {
            Some(found) => *value = Some(found),
            None => return Ok(false),
        }
    }
    Ok(true)
}

/// Store the values in the slots; a partial instance records which fields it
/// was given (`given`), a complete one all of them.
fn write<'py>(
    py: Python<'py>,
    class: &PlanClass,
    instance: &Bound<'py, PyAny>,
    values: Vec<Option<Bound<'py, PyAny>>>,
    partial: bool,
    given: impl FnOnce() -> PyResult<Bound<'py, PyAny>>,
) -> PyResult<()> {
    let Some(layout) = &class.layout else {
        return Ok(());
    };
    for (offset, value) in layout.offsets.iter().zip(values) {
        if let Some(value) = value {
            store(instance, *offset, value);
        }
    }
    if let (Some(offset), Some(all)) = (layout.supplied, &class.supplied) {
        let supplied = if partial {
            given()?
        } else {
            all.bind(py).clone()
        };
        store(instance, offset, supplied);
    }
    Ok(())
}

/// The instance a mapping of wire names describes, built by writing the
/// slots; `None` when anything is not vouched for (the general validator then
/// runs on the same data and reports what it always reported).
pub(crate) fn from_record<'py>(
    typed: &Typed,
    data: &Bound<'py, PyAny>,
) -> Option<Bound<'py, PyAny>> {
    record(typed, typed.root, data, 0)
}

fn record<'py>(
    typed: &Typed,
    index: usize,
    data: &Bound<'py, PyAny>,
    depth: usize,
) -> Option<Bound<'py, PyAny>> {
    let py = data.py();
    let class = &typed.classes[index];
    class.layout.as_ref()?;
    let dict = data.cast_exact::<PyDict>().ok()?;
    if depth >= MAX_DEPTH {
        return None;
    }
    let mut values: Vec<Option<Bound<'py, PyAny>>> = Vec::with_capacity(class.fields.len());
    let mut given = Vec::new();
    for field in &class.fields {
        match dict.get_item(field.wire.bind(py)).ok()? {
            Some(value) => {
                values.push(Some(read(typed, &field.plan, &value, depth + 1)?));
                given.push(field.name.bind(py).clone());
            }
            None => values.push(None),
        }
    }
    if given.len() != dict.len() {
        return None;
    }
    let partial = given.len() < class.fields.len();
    if partial && !fill_omitted(py, typed, class, &mut values).ok()? {
        return None;
    }
    let instance = super::allocate(class.class.bind(py)).ok()?;
    write(py, class, &instance, values, partial, || {
        Ok(PyFrozenSet::new(py, given)?.into_any())
    })
    .ok()?;
    Some(instance)
}

/// `value` for a field of `plan`, where a nested schema may be given as a
/// mapping (built here) and a list may hold such mappings.
fn read<'py>(
    typed: &Typed,
    plan: &Plan,
    value: &Bound<'py, PyAny>,
    depth: usize,
) -> Option<Bound<'py, PyAny>> {
    match &plan.shape {
        // `validate` takes mappings for nested schemas, never ready instances,
        // and always builds new lists (the input is not kept).
        Shape::Schema(index) if !value.is_none() => {
            let dict = value.cast_exact::<PyDict>().ok()?;
            record(typed, *index, dict.as_any(), depth)
        }
        Shape::List { min, max, item } if !value.is_none() => {
            let list = value.cast_exact::<PyList>().ok()?;
            within(list.len(), *min, *max).ok()?;
            let members = list
                .iter()
                .map(|member| read(typed, item, &member, depth))
                .collect::<Option<Vec<_>>>()?;
            Some(PyList::new(value.py(), members).ok()?.into_any())
        }
        _ => accept(typed, plan, value, depth),
    }
}

/// `instance` with `changes` applied, built by the class itself: the fields
/// the instance was given (all of them when it does not track that) with the
/// changes over them. `None` when a change names no field or the instance is
/// not exactly of the compiled class: `replace` then runs in Python.
pub(crate) fn replaced<'py>(
    typed: &Typed,
    instance: &Bound<'py, PyAny>,
    changes: &Bound<'py, PyDict>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = instance.py();
    let class = &typed.classes[typed.root];
    let kind = instance.get_type();
    if !kind.is(class.class.bind(py)) {
        return Ok(None);
    }
    for key in changes.keys() {
        let known = class
            .fields
            .iter()
            .any(|field| field.name.bind(py).as_any().eq(&key).unwrap_or(false));
        if !known {
            return Ok(None);
        }
    }
    let supplied = instance.getattr(typed.supplied.bind(py)).ok();
    let merged = PyDict::new(py);
    for field in &class.fields {
        let name = field.name.bind(py);
        if let Some(given) = supplied.as_ref().filter(|found| !found.is_none()) {
            if !given.contains(name)? {
                continue;
            }
        }
        merged.set_item(name, instance.getattr(name)?)?;
    }
    merged.update(changes.as_mapping())?;
    kind.call((), Some(&merged)).map(Some)
}

/// `value` as it must be stored, when the plan vouches for it.
fn accept<'py>(
    typed: &Typed,
    plan: &Plan,
    value: &Bound<'py, PyAny>,
    depth: usize,
) -> Option<Bound<'py, PyAny>> {
    if value.is_none() {
        return plan.nullable.then(|| value.clone());
    }
    match &plan.shape {
        Shape::Str { min, max } => {
            let text = value.cast_exact::<PyString>().ok()?;
            if text.to_str().ok()?.contains('\0') {
                return None;
            }
            within(value.len().ok()?, *min, *max).ok()?;
            Some(value.clone())
        }
        Shape::Int(bounds) => {
            let int = value.cast_exact::<PyInt>().ok()?.extract::<i64>().ok()?;
            bounds.holds(Number::Int(int)).then(|| value.clone())
        }
        Shape::Float(bounds) => accept_float(bounds, value),
        Shape::Bool => value
            .is_exact_instance_of::<PyBool>()
            .then(|| value.clone()),
        Shape::List { min, max, item } => {
            let list = value.cast_exact::<PyList>().ok()?;
            within(list.len(), *min, *max).ok()?;
            for member in list.iter() {
                if !accept(typed, item, &member, depth)?.is(&member) {
                    return None;
                }
            }
            Some(value.clone())
        }
        Shape::Schema(index) => accept_nested(typed, &typed.classes[*index], value, depth),
    }
}

fn accept_float<'py>(bounds: &Bounds, value: &Bound<'py, PyAny>) -> Option<Bound<'py, PyAny>> {
    if let Ok(float) = value.cast_exact::<PyFloat>() {
        let number = float.value();
        return (number.is_finite() && bounds.holds(Number::Float(number))).then(|| value.clone());
    }
    // An int given for a float field is stored as the float it denotes.
    let int = value.cast_exact::<PyInt>().ok()?.extract::<i64>().ok()?;
    if int.abs() > EXACT_INT {
        return None;
    }
    #[expect(clippy::cast_precision_loss, reason = "the magnitude is below 2^53")]
    let number = int as f64;
    bounds
        .holds(Number::Float(number))
        .then(|| PyFloat::new(value.py(), number).into_any())
}

/// A nested instance is judged by what it holds now, as the Python check does.
fn accept_nested<'py>(
    typed: &Typed,
    class: &PlanClass,
    value: &Bound<'py, PyAny>,
    depth: usize,
) -> Option<Bound<'py, PyAny>> {
    let py = value.py();
    if depth >= MAX_DEPTH || !value.get_type().is(class.class.bind(py)) {
        return None;
    }
    for field in &class.fields {
        let held = value.getattr(field.name.bind(py)).ok()?;
        if !accept(typed, &field.plan, &held, depth + 1)?.is(&held) {
            return None;
        }
    }
    Some(value.clone())
}
