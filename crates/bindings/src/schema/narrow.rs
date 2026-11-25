//! A nested `Schema` value is sent as the type its field declares.
//!
//! A field `inner: Public` holding a subclass instance of `Public` must send
//! the declared fields only, as a top-level `Public` response does. The
//! encoders know only the runtime value, so each schema class carries a
//! [`Guide`] per field (what the annotation declares) and calls [`narrow`] on
//! the value it just read: `None` when the value is already what is declared
//! (a pointer compare for an exact type), otherwise the value with every
//! subclass instance wrapped in a [`Narrowed`] that names the class to write
//! it as.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple, PyType};

use super::{Definition, Kind, Node, PyRules};

/// What a field annotation declares, as far as nested schemas go.
pub(crate) enum Guide {
    /// One class, or the members of a union: a value is written as the first
    /// declared class on its own MRO.
    Schema(Box<[Py<PyAny>]>),
    /// `list[X]` and `tuple[X, ...]` (a set or frozenset holds no schema: its
    /// members must be hashable scalars).
    Seq(Box<Guide>),
    /// `tuple[X, Y]`: a guide per position.
    Tuple(Box<[Option<Guide>]>),
    /// `dict[str, X]`.
    Map(Box<Guide>),
    /// A union of containers and schemas.
    Union(Box<[Guide]>),
}

/// A schema instance to be written as `declared`, a class it derives from.
#[pyclass(frozen, module = "hinoki.core")]
pub(crate) struct Narrowed {
    pub(crate) value: Py<PyAny>,
    pub(crate) declared: Py<PyAny>,
}

/// The guide of one field's node, `None` when it declares no schema.
pub(super) fn guide_of(
    py: Python<'_>,
    node: &Node<PyRules>,
    definitions: &[Definition<PyRules>],
) -> Option<Guide> {
    match &node.kind {
        Kind::Schema(index) => schema_class(py, definitions, *index)
            .map(|class| Guide::Schema(vec![class].into_boxed_slice())),
        Kind::TaggedUnion { variants, .. } => {
            let classes = variants
                .iter()
                .filter_map(|(_, index)| schema_class(py, definitions, *index))
                .collect::<Vec<_>>();
            (!classes.is_empty()).then(|| Guide::Schema(classes.into_boxed_slice()))
        }
        Kind::AnyOf(variants) => union_guide(py, variants, definitions),
        Kind::List(item) => guide_of(py, item, definitions).map(|guide| Guide::Seq(guide.into())),
        Kind::Map { value, .. } => {
            guide_of(py, value, definitions).map(|guide| Guide::Map(guide.into()))
        }
        Kind::Tuple(items) => {
            let guides = items
                .iter()
                .map(|item| guide_of(py, item, definitions))
                .collect::<Vec<_>>();
            guides
                .iter()
                .any(Option::is_some)
                .then(|| Guide::Tuple(guides.into_boxed_slice()))
        }
        _ => None,
    }
}

/// The declared class of a definition when it is a Hinoki `Schema`.
fn schema_class(
    py: Python<'_>,
    definitions: &[Definition<PyRules>],
    index: usize,
) -> Option<Py<PyAny>> {
    let class = definitions.get(index)?.class.object.bind(py);
    class
        .hasattr(intern!(py, "__hinoki_schema__"))
        .unwrap_or(false)
        .then(|| class.clone().unbind())
}

/// The schema members of a union merge into one guide, so a value is matched
/// against all of them and never crosses into another member's fields.
fn union_guide(
    py: Python<'_>,
    variants: &[Node<PyRules>],
    definitions: &[Definition<PyRules>],
) -> Option<Guide> {
    let mut classes = Vec::new();
    let mut others = Vec::new();
    for guide in variants
        .iter()
        .filter_map(|variant| guide_of(py, variant, definitions))
    {
        match guide {
            Guide::Schema(found) => classes.extend(found.into_vec()),
            other => others.push(other),
        }
    }
    if !classes.is_empty() {
        others.insert(0, Guide::Schema(classes.into_boxed_slice()));
    }
    match others.len() {
        0 => None,
        1 => others.pop(),
        _ => Some(Guide::Union(others.into_boxed_slice())),
    }
}

/// `value` as its declared form, or `None` when it is written as it is. Only a
/// strict subclass of a declared class changes.
pub(crate) fn narrow<'py>(
    value: &Bound<'py, PyAny>,
    guide: &Guide,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    match guide {
        Guide::Schema(classes) => narrow_instance(value, classes),
        Guide::Seq(inner) => narrow_sequence(value, inner),
        Guide::Tuple(guides) => narrow_tuple(value, guides),
        Guide::Map(inner) => narrow_map(value, inner),
        Guide::Union(members) => {
            for member in members.iter() {
                if let Some(narrowed) = narrow(value, member)? {
                    return Ok(Some(narrowed));
                }
            }
            Ok(None)
        }
    }
}

fn narrow_instance<'py>(
    value: &Bound<'py, PyAny>,
    classes: &[Py<PyAny>],
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if value.is_none() || is_declared(value, classes) {
        return Ok(None);
    }
    let py = value.py();
    let mro = value.get_type().getattr(intern!(py, "__mro__"))?;
    for base in mro.cast::<PyTuple>()?.iter().skip(1) {
        if classes.iter().any(|class| class.as_ptr() == base.as_ptr()) {
            let wrapped = Narrowed {
                value: value.clone().unbind(),
                declared: base.unbind(),
            };
            return Ok(Some(Bound::new(py, wrapped)?.into_any()));
        }
    }
    Ok(None)
}

/// The items of a list or tuple narrowed one by one; a new container only when
/// one changed (an unchanged one costs a walk and no allocation).
fn narrow_sequence<'py>(
    value: &Bound<'py, PyAny>,
    inner: &Guide,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if let (Guide::Schema(classes), Ok(list)) = (inner, value.cast::<PyList>()) {
        // The usual case, every item exactly a declared class: a pointer compare each.
        if list.iter().all(|item| is_declared(&item, classes)) {
            return Ok(None);
        }
    }
    narrow_positions(value, |_| Some(inner))
}

fn is_declared(value: &Bound<'_, PyAny>, classes: &[Py<PyAny>]) -> bool {
    let kind = value.get_type_ptr().cast::<pyo3::ffi::PyObject>();
    classes.iter().any(|class| class.as_ptr() == kind)
}

fn narrow_tuple<'py>(
    value: &Bound<'py, PyAny>,
    guides: &[Option<Guide>],
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if !value.is_instance_of::<PyTuple>() {
        return Ok(None);
    }
    narrow_positions(value, |at| guides.get(at).and_then(Option::as_ref))
}

fn narrow_positions<'py, 'g>(
    value: &Bound<'py, PyAny>,
    guide_at: impl Fn(usize) -> Option<&'g Guide> + Copy,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if let Ok(list) = value.cast::<PyList>() {
        return narrow_walk(value, || list.iter(), guide_at);
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        return narrow_walk(value, || tuple.iter(), guide_at);
    }
    Ok(None)
}

/// The first item that changes decides: only then is the new container built.
fn narrow_walk<'py, 'g, I: Iterator<Item = Bound<'py, PyAny>>>(
    value: &Bound<'py, PyAny>,
    items: impl Fn() -> I,
    guide_at: impl Fn(usize) -> Option<&'g Guide>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let narrow_at = |at: usize, item: &Bound<'py, PyAny>| match guide_at(at) {
        Some(guide) => narrow(item, guide),
        None => Ok(None),
    };
    let mut first = None;
    for (at, item) in items().enumerate() {
        if let Some(new) = narrow_at(at, &item)? {
            first = Some((at, new));
            break;
        }
    }
    let Some((changed, new)) = first else {
        return Ok(None);
    };
    let mut out = Vec::new();
    for (at, item) in items().enumerate() {
        out.push(match at.cmp(&changed) {
            std::cmp::Ordering::Less => item,
            std::cmp::Ordering::Equal => new.clone(),
            std::cmp::Ordering::Greater => narrow_at(at, &item)?.unwrap_or(item),
        });
    }
    rebuild(value, out)
}

/// The kind of container `value` is, holding `items`.
fn rebuild<'py>(
    value: &Bound<'py, PyAny>,
    items: Vec<Bound<'py, PyAny>>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = value.py();
    if value.is_instance_of::<PyTuple>() {
        return Ok(Some(PyTuple::new(py, items)?.into_any()));
    }
    Ok(Some(PyList::new(py, items)?.into_any()))
}

fn narrow_map<'py>(
    value: &Bound<'py, PyAny>,
    inner: &Guide,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Ok(map) = value.cast::<PyDict>() else {
        return Ok(None);
    };
    let mut changed = false;
    let mut pairs = Vec::with_capacity(map.len());
    for (key, item) in map.iter() {
        let narrowed = narrow(&item, inner)?;
        changed |= narrowed.is_some();
        pairs.push((key, narrowed.unwrap_or(item)));
    }
    if !changed {
        return Ok(None);
    }
    let out = PyDict::new(value.py());
    for (key, item) in pairs {
        out.set_item(key, item)?;
    }
    Ok(Some(out.into_any()))
}

/// The class a value is written as when it is a [`Narrowed`].
pub(crate) fn narrowed_parts<'py>(
    value: &Bound<'py, PyAny>,
) -> Option<(Bound<'py, PyAny>, Bound<'py, PyType>)> {
    let narrowed = value.cast_exact::<Narrowed>().ok()?;
    let narrowed = narrowed.get();
    let py = value.py();
    let declared = narrowed.declared.bind(py).cast::<PyType>().ok()?.clone();
    Some((narrowed.value.bind(py).clone(), declared))
}
