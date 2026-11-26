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
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};

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
    /// A `TypedDict`: a guide per key.
    Keyed(Box<[(String, Guide)]>),
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
        Guide::Keyed(keys) => narrow_keyed(value, keys),
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
    let mut class = value.get_type();
    if class.is(crate::pytypes::projected_item_class(py)?) {
        // A lazy projection reports its Schema as its class (`__class__`), but
        // its own type is the wrapper: read the Schema it stands for.
        class = value
            .getattr(intern!(py, "_schema"))?
            .cast_into::<PyType>()?;
        if classes.iter().any(|known| known.as_ptr() == class.as_ptr()) {
            return Ok(None); // pre-encoded as exactly the declared Schema
        }
    }
    let mro = class.getattr(intern!(py, "__mro__"))?;
    for base in mro.cast::<PyTuple>()?.iter().skip(1) {
        if classes.iter().any(|known| known.as_ptr() == base.as_ptr()) {
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
    if let Guide::Schema(classes) = inner {
        // The usual case, every item exactly a declared class: a pointer compare each.
        let exact = if let Ok(list) = value.cast_exact::<PyList>() {
            list.iter().all(|item| is_declared(&item, classes))
        } else if let Ok(tuple) = value.cast::<PyTuple>() {
            tuple.iter().all(|item| is_declared(&item, classes))
        } else {
            keeps_lazy_projection(value, classes)?
        };
        if exact {
            return Ok(None);
        }
    }
    narrow_positions(value, |_| Some(inner))
}

/// A lazy projection still holding its rows has no items in its list storage:
/// it is pre-encoded as the Schema it was projected to, which is kept when that
/// is a declared class (or none of the declared classes is its base).
fn keeps_lazy_projection(value: &Bound<'_, PyAny>, classes: &[Py<PyAny>]) -> PyResult<bool> {
    let py = value.py();
    if !value
        .get_type()
        .is(crate::pytypes::projected_list_class(py)?)
        || value.getattr(intern!(py, "_projected_handle"))?.is_none()
    {
        return Ok(false);
    }
    let schema = value
        .getattr(intern!(py, "_schema"))?
        .cast_into::<PyType>()?;
    let mro = schema.getattr(intern!(py, "__mro__"))?;
    let strict_subclass = mro
        .cast::<PyTuple>()?
        .iter()
        .skip(1)
        .any(|base| classes.iter().any(|known| known.as_ptr() == base.as_ptr()));
    Ok(!strict_subclass)
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
    if let Ok(list) = value.cast_exact::<PyList>() {
        return narrow_walk(value, || list.iter(), guide_at);
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        return narrow_walk(value, || tuple.iter(), guide_at);
    }
    if value.is_instance_of::<PyList>() {
        // A list subclass may keep its items elsewhere than the list storage (a
        // lazy projection materialises on iteration): read them through Python.
        let items = value.try_iter()?.collect::<PyResult<Vec<_>>>()?;
        return narrow_walk(value, || items.iter().cloned(), guide_at);
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
    if let Guide::Schema(classes) = inner {
        if map.iter().all(|(_, item)| is_declared(&item, classes)) {
            return Ok(None);
        }
    }
    // Nothing is allocated until a value changes.
    let mut first = None;
    for (key, item) in map.iter() {
        if let Some(new) = narrow(&item, inner)? {
            first = Some((key, new));
            break;
        }
    }
    let Some((changed, new)) = first else {
        return Ok(None);
    };
    let out = PyDict::new(value.py());
    for (key, item) in map.iter() {
        if key.is(&changed) {
            out.set_item(key, &new)?;
        } else {
            out.set_item(key, narrow(&item, inner)?.unwrap_or(item))?;
        }
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

fn narrow_keyed<'py>(
    value: &Bound<'py, PyAny>,
    keys: &[(String, Guide)],
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Ok(map) = value.cast::<PyDict>() else {
        return Ok(None);
    };
    let mut changed = false;
    let mut pairs = Vec::with_capacity(map.len());
    for (key, item) in map.iter() {
        let guide = key
            .cast::<PyString>()
            .ok()
            .and_then(|name| {
                keys.iter()
                    .find(|(known, _)| name.to_str().is_ok_and(|n| n == known))
            })
            .map(|(_, guide)| guide);
        let narrowed = match guide {
            Some(guide) => narrow(&item, guide)?,
            None => None,
        };
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

#[pymethods]
impl Narrowed {
    /// The declared form as a dict, for a renderer that reads plain data.
    fn dump<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let value = self.value.bind(py);
        let declared = self.declared.bind(py).cast::<PyType>()?;
        match super::serialize::dump_as(value, declared)? {
            Some(dumped) => Ok(dumped.into_any()),
            None => value.call_method0("dump"),
        }
    }
}

/// The guide of a handler's return annotation, built from a nested spec:
/// `("schema", [classes])`, `("seq", spec)`, `("tuple", [spec | None])`,
/// `("map", spec)`, `("keyed", [(key, spec)])` or `("union", [specs])`.
#[pyclass(frozen, module = "hinoki.core", name = "_ReturnGuide")]
pub struct ReturnGuide {
    guide: Guide,
}

#[pymethods]
impl ReturnGuide {
    #[new]
    fn new(spec: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self {
            guide: from_spec(spec)?,
        })
    }

    /// `value` with every subclass of a declared class narrowed to it; the
    /// same object when nothing changes.
    fn narrow<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        Ok(narrow(value, &self.guide)?.unwrap_or_else(|| value.clone()))
    }
}

pub(super) fn from_spec(spec: &Bound<'_, PyAny>) -> PyResult<Guide> {
    let (kind, arg): (String, Bound<'_, PyAny>) = spec.extract()?;
    let bad = || pyo3::exceptions::PyValueError::new_err(format!("unknown guide {kind}"));
    Ok(match kind.as_str() {
        "schema" => Guide::Schema(arg.extract::<Vec<Py<PyAny>>>()?.into_boxed_slice()),
        "seq" => Guide::Seq(from_spec(&arg)?.into()),
        "map" => Guide::Map(from_spec(&arg)?.into()),
        "union" => Guide::Union(specs(&arg)?.into_boxed_slice()),
        "tuple" => {
            let mut guides = Vec::new();
            for item in arg.try_iter()? {
                let item = item?;
                guides.push(if item.is_none() {
                    None
                } else {
                    Some(from_spec(&item)?)
                });
            }
            Guide::Tuple(guides.into_boxed_slice())
        }
        "keyed" => {
            let mut keys = Vec::new();
            for entry in arg.try_iter()? {
                let (key, inner): (String, Bound<'_, PyAny>) = entry?.extract()?;
                keys.push((key, from_spec(&inner)?));
            }
            Guide::Keyed(keys.into_boxed_slice())
        }
        _ => return Err(bad()),
    })
}

fn specs(arg: &Bound<'_, PyAny>) -> PyResult<Vec<Guide>> {
    arg.try_iter()?.map(|item| from_spec(&item?)).collect()
}
