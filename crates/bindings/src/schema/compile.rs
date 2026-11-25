use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple, PyType};
use regex::Regex;

use super::internet::NetKind;
use super::model::{
    Choice, Collection, Compiled, Definition, Direct, Field, FieldGuides, FieldTable,
    JsonFieldEntry, JsonFieldPlan, Kind, LimitValue, NativeChoices, NativeLimit, NativeRules,
    NativeSchema, Node, Normalize, Number, PyClass, PyRules, Rules, SchemaInstanceFields,
    SchemaWireNames,
};
use super::scalars::DecimalNumber;
use super::typeddecode;

fn compile_error(message: impl Into<String>) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(message.into())
}

fn optional_int(constraints: &Bound<'_, PyDict>, name: &str) -> PyResult<Option<i64>> {
    constraints
        .get_item(name)?
        .map(|v| v.extract::<i64>())
        .transpose()
}

pub(super) fn flag(constraints: &Bound<'_, PyDict>, name: &str) -> PyResult<bool> {
    constraints
        .get_item(name)?
        .map_or(Ok(false), |v| v.extract::<bool>())
}

fn optional_object(constraints: &Bound<'_, PyDict>, name: &str) -> PyResult<Option<Py<PyAny>>> {
    Ok(constraints.get_item(name)?.map(Bound::unbind))
}

fn optional_choices(constraints: &Bound<'_, PyDict>) -> PyResult<Option<Vec<Py<PyAny>>>> {
    let Some(values) = constraints.get_item("one_of")? else {
        return Ok(None);
    };
    let mut choices = Vec::new();
    for value in values.try_iter()? {
        choices.push(value?.unbind());
    }
    Ok(Some(choices))
}

fn parse_node(raw: &Bound<'_, PyAny>) -> PyResult<Node<PyRules>> {
    let (kind, nullable, constraints, item, index): (
        String,
        bool,
        Bound<'_, PyDict>,
        Option<Bound<'_, PyAny>>,
        Option<usize>,
    ) = raw.extract()?;
    let kind = match kind.as_str() {
        "str" => Kind::Str,
        "int" => Kind::Int,
        "float" => Kind::Float,
        "bool" => Kind::Bool,
        "uuid" => Kind::Uuid,
        "datetime" => Kind::Datetime,
        "date" => Kind::Date,
        "time" => Kind::Time,
        "timedelta" => Kind::Timedelta,
        "decimal" => Kind::Decimal,
        "bytes" => Kind::Bytes,
        "ipv4" => Kind::Net(NetKind::V4),
        "ipv6" => Kind::Net(NetKind::V6),
        "ipv4network" => Kind::Net(NetKind::V4Network),
        "ipv6network" => Kind::Net(NetKind::V6Network),
        "path" => Kind::Path,
        "secret" => Kind::Secret,
        "list" => {
            let inner = item.ok_or_else(|| compile_error("list type without an item type"))?;
            Kind::List(Box::new(parse_node(&inner)?))
        }
        "any" => Kind::Any,
        "anyof" => {
            let variants = constraints
                .get_item("items")?
                .ok_or_else(|| compile_error("union type without variants"))?;
            let mut nodes = Vec::new();
            for variant in variants.try_iter()? {
                nodes.push(parse_node(&variant?)?);
            }
            Kind::AnyOf(nodes)
        }
        "tuple" => {
            let positions = constraints
                .get_item("items")?
                .ok_or_else(|| compile_error("tuple type without item types"))?;
            let mut nodes = Vec::new();
            for position in positions.try_iter()? {
                nodes.push(parse_node(&position?)?);
            }
            Kind::Tuple(nodes)
        }
        "dict" => {
            let value = item.ok_or_else(|| compile_error("dict type without a value type"))?;
            let key = constraints
                .get_item("key")?
                .ok_or_else(|| compile_error("dict type without a key type"))?;
            Kind::Map {
                key: Box::new(parse_node(&key)?),
                value: Box::new(parse_node(&value)?),
            }
        }
        "schema" => {
            Kind::Schema(index.ok_or_else(|| compile_error("schema type without a schema"))?)
        }
        "union" => {
            let discriminator = required_string(&constraints, "discriminator")?;
            let discriminator_wire_name = required_string(&constraints, "discriminator_wire_name")?;
            let variants = required_variants(&constraints)?;
            Kind::TaggedUnion {
                discriminator,
                discriminator_wire_name,
                variants,
            }
        }
        other => return Err(compile_error(format!("unknown field kind {other:?}"))),
    };
    let pattern = match constraints.get_item("pattern")? {
        Some(text) => {
            let source: String = text.extract()?;
            let regex = Regex::new(&format!("\\A(?:{source})\\z"))
                .map_err(|e| compile_error(format!("unsupported pattern {source:?}: {e}")))?;
            Some((text.unbind(), regex))
        }
        None => None,
    };
    let collection = match constraints.get_item("collection")? {
        Some(name) => Some(match name.extract::<String>()?.as_str() {
            "set" => Collection::Set,
            "frozenset" => Collection::Frozenset,
            "tuple" => Collection::Tuple,
            other => return Err(compile_error(format!("unknown collection {other:?}"))),
        }),
        None => None,
    };
    Ok(Node {
        kind,
        collection,
        nullable,
        min_length: optional_int(&constraints, "min_length")?,
        max_length: optional_int(&constraints, "max_length")?,
        input_max_length: optional_int(&constraints, "input_max_length")?,
        pattern,
        normalize: Normalize::from_constraints(&constraints)?,
        ge: optional_object(&constraints, "ge")?,
        le: optional_object(&constraints, "le")?,
        gt: optional_object(&constraints, "gt")?,
        lt: optional_object(&constraints, "lt")?,
        one_of: optional_choices(&constraints)?,
        member: optional_object(&constraints, "enum")?,
        max_digits: optional_int(&constraints, "max_digits")?,
        decimal_places: optional_int(&constraints, "decimal_places")?,
        multiple_of: optional_object(&constraints, "multiple_of")?,
    })
}

fn required_string(constraints: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    constraints
        .get_item(key)?
        .ok_or_else(|| compile_error(format!("union type without {key}")))?
        .extract()
}

fn required_variants(constraints: &Bound<'_, PyDict>) -> PyResult<Vec<(String, usize)>> {
    let values = constraints
        .get_item("variants")?
        .ok_or_else(|| compile_error("union type without variants"))?;
    let mut variants = Vec::new();
    for value in values.try_iter()? {
        let variant = value?;
        let (tag, index): (String, usize) = variant.extract()?;
        if variants.iter().any(|(known, _)| known == &tag) {
            return Err(compile_error("union type has duplicate discriminator tags"));
        }
        variants.push((tag, index));
    }
    if variants.len() < 2 {
        return Err(compile_error("union type needs at least two variants"));
    }
    Ok(variants)
}

/// Build a validator from `definitions`: a list of
/// `(class position, supplied-fields value, defaults, default factories)`.
type DirectEntry = (usize, Option<Py<PyAny>>, Py<PyDict>, Py<PyDict>);

/// `(class, [(name, wire_name, node, required), ...], forbid_extra)`; nested
/// schemas refer to a definition by index.
#[pyfunction]
#[pyo3(signature = (definitions, root, uuid_class, from_iso, unset, direct=None))]
pub fn compile_schemas(
    py: Python<'_>,
    definitions: Vec<(Py<PyAny>, FieldTable<'_>, bool)>,
    root: usize,
    uuid_class: Py<PyAny>,
    from_iso: Py<PyAny>,
    unset: Py<PyAny>,
    direct: Option<Vec<DirectEntry>>,
) -> PyResult<Compiled> {
    let mut direct = direct.unwrap_or_default();
    let mut built = Vec::with_capacity(definitions.len());
    for (position, (class, fields, forbid_extra)) in definitions.into_iter().enumerate() {
        let direct = direct
            .iter()
            .position(|(index, ..)| *index == position)
            .map(|at| {
                let (_, supplied, values, factories) = direct.swap_remove(at);
                Direct {
                    fields: fields.len(),
                    supplied,
                    index: position,
                    defaults: Some((values, factories)),
                }
            });
        let mut parsed = Vec::with_capacity(fields.len());
        for (name, wire_name, node, required) in fields {
            parsed.push(Field {
                name,
                wire_name,
                node: parse_node(&node)?,
                required,
            });
        }
        built.push(Definition {
            class: PyClass {
                object: class,
                direct,
            },
            fields: parsed,
            forbid_extra,
        });
    }
    if root >= built.len() {
        return Err(compile_error("root schema is not in the table"));
    }
    let known = built.len();
    for definition in &built {
        for field in &definition.fields {
            if refers_outside(&field.node, known) {
                return Err(compile_error("a schema refers to a missing definition"));
            }
        }
    }
    let native = lower_schemas(py, &built, root);
    let typed = native
        .as_deref()
        .and_then(|lowered| typeddecode::typed(py, &built, lowered, root))
        .map(Arc::new);
    let guides = built[root]
        .fields
        .iter()
        .map(|field| super::narrow::guide_of(py, &field.node, &built))
        .collect();
    Ok(Compiled {
        definitions: built,
        root,
        uuid: uuid_class,
        from_iso,
        unset,
        native,
        typed,
        guides,
        json_plan: PyOnceLock::new(),
    })
}

/// A constraint that has no exact plain-Rust equivalent.
struct Unlowerable;

/// The GIL-free copy of `definitions`, or `None` when a constraint is not an
/// exact builtin scalar the Rust side compares exactly as Python does.
fn lower_schemas(
    py: Python<'_>,
    definitions: &[Definition<PyRules>],
    root: usize,
) -> Option<Arc<NativeSchema>> {
    let definitions = definitions
        .iter()
        .map(|definition| lower_definition(py, definition))
        .collect::<Result<Vec<_>, Unlowerable>>()
        .ok()?;
    Some(Arc::new(NativeSchema { definitions, root }))
}

fn lower_definition(
    py: Python<'_>,
    definition: &Definition<PyRules>,
) -> Result<Definition<NativeRules>, Unlowerable> {
    let fields = definition
        .fields
        .iter()
        .map(|field| {
            Ok(Field {
                name: field.name.clone(),
                wire_name: field.wire_name.clone(),
                node: lower_node(py, &field.node)?,
                required: field.required,
            })
        })
        .collect::<Result<Vec<_>, Unlowerable>>()?;
    Ok(Definition {
        class: (),
        fields,
        forbid_extra: definition.forbid_extra,
    })
}

fn lower_kind(py: Python<'_>, kind: &Kind<PyRules>) -> Result<Kind<NativeRules>, Unlowerable> {
    Ok(match kind {
        Kind::Str => Kind::Str,
        Kind::Int => Kind::Int,
        Kind::Float => Kind::Float,
        Kind::Bool => Kind::Bool,
        Kind::Uuid => Kind::Uuid,
        Kind::Datetime => Kind::Datetime,
        Kind::Date => Kind::Date,
        Kind::Time => Kind::Time,
        Kind::Timedelta => Kind::Timedelta,
        Kind::Decimal => Kind::Decimal,
        Kind::Bytes => Kind::Bytes,
        Kind::Net(kind) => Kind::Net(*kind),
        Kind::List(item) => Kind::List(Box::new(lower_node(py, item)?)),
        Kind::Tuple(_) | Kind::Any | Kind::AnyOf(_) | Kind::Path | Kind::Secret => {
            return Err(Unlowerable)
        }
        Kind::Map { key, value } => Kind::Map {
            key: Box::new(lower_node(py, key)?),
            value: Box::new(lower_node(py, value)?),
        },
        Kind::Schema(index) => Kind::Schema(*index),
        Kind::TaggedUnion {
            discriminator,
            discriminator_wire_name,
            variants,
        } => Kind::TaggedUnion {
            discriminator: discriminator.clone(),
            discriminator_wire_name: discriminator_wire_name.clone(),
            variants: variants.clone(),
        },
    })
}

fn lower_node(py: Python<'_>, node: &Node<PyRules>) -> Result<Node<NativeRules>, Unlowerable> {
    let kind = lower_kind(py, &node.kind)?;
    let scalar = matches!(kind, Kind::Str | Kind::Int | Kind::Float | Kind::Bool);
    // Only strings consult `pattern` and only numbers their bounds; a
    // constraint the walk never reads needs no Rust equivalent.
    let numeric = matches!(kind, Kind::Int | Kind::Float | Kind::Decimal);
    let decimal = matches!(kind, Kind::Decimal);
    let pattern = match &node.pattern {
        Some((source, regex)) if matches!(kind, Kind::Str | Kind::Bytes) => {
            Some((lower_pattern(source.bind(py))?, regex.clone()))
        }
        _ => None,
    };
    let limit = |limit: &Option<Py<PyAny>>| match limit {
        Some(limit) if decimal => lower_decimal_limit(py, limit).map(Some),
        Some(limit) if numeric => lower_limit(py, limit).map(Some),
        _ => Ok(None),
    };
    let one_of = match &node.one_of {
        // Other kinds compare Python objects (UUID, datetime, dict, instance).
        Some(_) if !scalar => return Err(Unlowerable),
        Some(choices) => Some(lower_choices(py, choices)?),
        None => None,
    };
    // A member is a Python object; the GIL-free walk cannot build one.
    // A set of scalars is built from the checked array without Python; any other
    // container, like a tuple, needs one.
    let collection = match node.collection {
        None => None,
        Some(kind @ (Collection::Set | Collection::Frozenset)) => Some(kind),
        Some(Collection::Tuple) => return Err(Unlowerable),
    };
    if node.member.is_some() || matches!(kind, Kind::Tuple(_)) {
        return Err(Unlowerable);
    }
    Ok(Node {
        kind,
        collection,
        nullable: node.nullable,
        min_length: node.min_length,
        max_length: node.max_length,
        input_max_length: node.input_max_length,
        pattern,
        normalize: node.normalize,
        ge: limit(&node.ge)?,
        le: limit(&node.le)?,
        gt: limit(&node.gt)?,
        lt: limit(&node.lt)?,
        one_of,
        member: None,
        max_digits: node.max_digits,
        decimal_places: node.decimal_places,
        multiple_of: limit(&node.multiple_of)?,
    })
}

fn lower_pattern(source: &Bound<'_, PyAny>) -> Result<String, Unlowerable> {
    if !source.is_exact_instance_of::<PyString>() {
        return Err(Unlowerable);
    }
    source
        .repr()
        .map(|shown| shown.to_string())
        .map_err(|_| Unlowerable)
}

fn exact_number(value: &Bound<'_, PyAny>) -> Result<Number, Unlowerable> {
    if value.is_exact_instance_of::<PyInt>() {
        return value
            .extract::<i64>()
            .map(Number::Int)
            .map_err(|_| Unlowerable);
    }
    if value.is_exact_instance_of::<PyFloat>() {
        return value
            .extract::<f64>()
            .map(Number::Float)
            .map_err(|_| Unlowerable);
    }
    Err(Unlowerable)
}

fn lower_limit(py: Python<'_>, limit: &Py<PyAny>) -> Result<NativeLimit, Unlowerable> {
    let limit = limit.bind(py);
    let value = LimitValue::Number(exact_number(limit)?);
    let shown = limit.str().map_err(|_| Unlowerable)?.to_string();
    Ok(NativeLimit { value, shown })
}

/// Digits past which a `Decimal` bound is left to Python's own comparison
/// rather than written out.
const MAX_LOWERED_DECIMAL_DIGITS: i64 = 1100;

/// A `Decimal` field's bound: an exact `int`, or an exact finite `Decimal`
/// of moderate size, written out in fixed-point.
fn lower_decimal_limit(py: Python<'_>, limit: &Py<PyAny>) -> Result<NativeLimit, Unlowerable> {
    let limit = limit.bind(py);
    let shown = limit.str().map_err(|_| Unlowerable)?.to_string();
    let text: String = if limit.is_exact_instance_of::<PyInt>() {
        shown.clone()
    } else if limit
        .get_type()
        .is(crate::pytypes::decimal_class(py).map_err(|_| Unlowerable)?)
    {
        let adjusted: i64 = limit
            .call_method0("adjusted")
            .and_then(|v| v.extract())
            .map_err(|_| Unlowerable)?;
        if adjusted.abs() > MAX_LOWERED_DECIMAL_DIGITS {
            return Err(Unlowerable);
        }
        let exponent: i64 = limit
            .call_method0("as_tuple")
            .and_then(|t| t.getattr("exponent"))
            .and_then(|v| v.extract())
            .map_err(|_| Unlowerable)?;
        if exponent.abs() > MAX_LOWERED_DECIMAL_DIGITS {
            return Err(Unlowerable);
        }
        limit
            .call_method1("__format__", ("f",))
            .and_then(|v| v.extract())
            .map_err(|_| Unlowerable)?
    } else {
        return Err(Unlowerable);
    };
    let value = DecimalNumber::parse(&text).ok_or(Unlowerable)?;
    Ok(NativeLimit {
        value: LimitValue::Decimal(value),
        shown,
    })
}

fn exact_choice(value: &Bound<'_, PyAny>) -> Result<Choice, Unlowerable> {
    if let Ok(flag) = value.cast_exact::<PyBool>() {
        return Ok(Choice::Number(Number::Int(i64::from(flag.is_true()))));
    }
    if value.is_exact_instance_of::<PyString>() {
        // Fails for lone surrogates, which a Rust string cannot hold.
        return value
            .extract::<String>()
            .map(Choice::Str)
            .map_err(|_| Unlowerable);
    }
    exact_number(value).map(Choice::Number)
}

fn lower_choices(py: Python<'_>, choices: &[Py<PyAny>]) -> Result<NativeChoices, Unlowerable> {
    let values = choices
        .iter()
        .map(|choice| exact_choice(choice.bind(py)))
        .collect::<Result<Vec<_>, Unlowerable>>()?;
    Ok(NativeChoices {
        values,
        shown: shown_choices(py, choices),
    })
}

/// The `one_of` message text: the `repr()` of the first ten choices.
pub(super) fn shown_choices(py: Python<'_>, choices: &[Py<PyAny>]) -> String {
    let shown: Vec<String> = choices
        .iter()
        .take(10)
        .map(|c| c.bind(py).repr().map(|r| r.to_string()).unwrap_or_default())
        .collect();
    shown.join(", ")
}

fn has_custom_dump(
    py: Python<'_>,
    class: &Bound<'_, PyType>,
    table: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let key = intern!(py, "__native_dump__");
    match table.get_item(key) {
        Ok(flag) => Ok(!flag.is_truthy()?),
        Err(_) => {
            class.call_method0(intern!(py, "native_schema"))?;
            Ok(!class
                .getattr(intern!(py, "__dict__"))?
                .get_item(key)?
                .is_truthy()?)
        }
    }
}

fn resolve_compiled<'py>(
    py: Python<'py>,
    class: &Bound<'py, PyType>,
    table: &Bound<'py, PyAny>,
    honour_custom_dump: bool,
) -> PyResult<Option<PyRef<'py, Compiled>>> {
    if honour_custom_dump && has_custom_dump(py, class, table)? {
        return Ok(None);
    }
    let own = table.get_item(intern!(py, "__native__")).ok();
    let compiled = match own {
        Some(found) if !found.is_none() => found,
        _ => class.call_method0(intern!(py, "native_schema"))?,
    };
    Ok(Some(compiled.extract()?))
}

fn member_descriptor_type<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    static TYPE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    TYPE.import(py, "types", "MemberDescriptorType")
}

fn object_getattribute<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    static ATTR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    ATTR.get_or_try_init(py, || {
        Ok(py
            .import("builtins")?
            .getattr("object")?
            .getattr("__getattribute__")?
            .unbind())
    })
    .map(|obj| obj.bind(py))
}

fn has_custom_descriptor(
    py: Python<'_>,
    mro: &Bound<'_, PyTuple>,
    attr: &Bound<'_, PyString>,
    member_desc: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let dict_key = intern!(py, "__dict__");
    let get_key = intern!(py, "__get__");
    for base in mro.iter() {
        let table = base.getattr(dict_key)?;
        if let Ok(found) = table.get_item(attr) {
            return Ok(found.hasattr(get_key)? && !found.is_instance(member_desc)?);
        }
    }
    Ok(false)
}

fn class_is_streamable(
    py: Python<'_>,
    class: &Bound<'_, PyType>,
    entries: &[JsonFieldEntry],
) -> PyResult<bool> {
    if class.hasattr(intern!(py, "__getattr__"))? {
        return Ok(false);
    }
    if !class
        .getattr(intern!(py, "__getattribute__"))?
        .is(object_getattribute(py)?)
    {
        return Ok(false);
    }
    let mro_obj = class.getattr(intern!(py, "__mro__"))?;
    let mro = mro_obj.cast::<PyTuple>()?;
    let member_desc = member_descriptor_type(py)?;
    for entry in entries {
        if has_custom_descriptor(py, mro, entry.attr.bind(py), member_desc)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn same_computed(cached: Option<&Py<PyAny>>, current: Option<&Bound<'_, PyAny>>) -> bool {
    match (cached, current) {
        (None, None) => true,
        (Some(a), Some(b)) => b.is(a),
        _ => false,
    }
}

fn build_json_field_plan(
    py: Python<'_>,
    class: &Bound<'_, PyType>,
    compiled: &Compiled,
    computed_obj: Option<&Bound<'_, PyAny>>,
    quote_key: fn(&str) -> Box<[u8]>,
) -> PyResult<JsonFieldPlan> {
    let declared = &compiled.definitions[compiled.root].fields;
    let mut entries = Vec::with_capacity(declared.len());
    for field in declared {
        entries.push(JsonFieldEntry {
            attr: PyString::intern(py, &field.name).unbind(),
            wire_name: field.wire_name.clone(),
            quoted_key: quote_key(&field.wire_name),
        });
    }
    let mut has_computed = false;
    if let Some(computed) = computed_obj {
        for entry in computed.try_iter()? {
            let (name, wire_name): (String, String) = entry?.extract()?;
            has_computed = true;
            entries.push(JsonFieldEntry {
                attr: PyString::intern(py, &name).unbind(),
                quoted_key: quote_key(&wire_name),
                wire_name,
            });
        }
    }
    let streamable = !has_computed && class_is_streamable(py, class, &entries)?;
    Ok(JsonFieldPlan {
        entries: entries.into_boxed_slice(),
        unset: compiled.unset.clone_ref(py),
        streamable,
        guided: compiled.guides.iter().any(Option::is_some),
        guides: Arc::clone(&compiled.guides),
        computed_source: computed_obj.map(|obj| obj.clone().unbind()),
    })
}

/// Cached JSON encoding plan for a `Schema` instance's class, or `None` when
/// `value` is not a schema instance or its class overrides `dump()`.
pub(crate) fn json_field_plan(
    value: &Bound<'_, PyAny>,
    quote_key: fn(&str) -> Box<[u8]>,
) -> PyResult<Option<Arc<JsonFieldPlan>>> {
    json_field_plan_of(&value.get_type(), quote_key)
}

/// The same plan for a class (the declared class of a narrowed value).
pub(crate) fn json_field_plan_of(
    class: &Bound<'_, PyType>,
    quote_key: fn(&str) -> Box<[u8]>,
) -> PyResult<Option<Arc<JsonFieldPlan>>> {
    let py = class.py();
    if !class.hasattr(intern!(py, "__hinoki_schema__"))? {
        return Ok(None);
    }
    let table = class.getattr(intern!(py, "__dict__"))?;
    let Some(compiled) = resolve_compiled(py, class, &table, true)? else {
        return Ok(None);
    };
    let current_computed = table.get_item(intern!(py, "__hinoki_computed__")).ok();
    let cached = compiled.json_plan.get_or_try_init(py, || {
        build_json_field_plan(py, class, &compiled, current_computed.as_ref(), quote_key)
            .map(Arc::new)
    })?;
    if same_computed(cached.computed_source.as_ref(), current_computed.as_ref()) {
        Ok(Some(Arc::clone(cached)))
    } else {
        Ok(Some(Arc::new(build_json_field_plan(
            py,
            class,
            &compiled,
            current_computed.as_ref(),
            quote_key,
        )?)))
    }
}

/// For a `Schema` instance: its field names in declaration order and the
/// `UNSET` sentinel (fields holding it are not serialised). The class is
/// compiled on first use. `None` when `value` is not a schema instance (or,
/// with `honour_custom_dump`, when its class overrides `dump()`).
pub fn instance_fields(
    value: &Bound<'_, PyAny>,
    honour_custom_dump: bool,
    by_alias: bool,
    with_computed: bool,
) -> PyResult<SchemaInstanceFields> {
    let class = value.get_type();
    Ok(
        class_fields(&class, honour_custom_dump, by_alias, with_computed)?
            .map(|(names, unset, extra, _)| (names, unset, extra)),
    )
}

/// What [`instance_fields`] reads from a class, and the guides of its declared fields.
pub(crate) type ClassFields = Option<(SchemaWireNames, Py<PyAny>, usize, FieldGuides)>;

/// The fields of a schema class (`instance_fields` of one of its instances).
pub(crate) fn class_fields(
    class: &Bound<'_, PyType>,
    honour_custom_dump: bool,
    by_alias: bool,
    with_computed: bool,
) -> PyResult<ClassFields> {
    let py = class.py();
    if !class.hasattr(intern!(py, "__hinoki_schema__"))? {
        return Ok(None);
    }
    // Read the class's own entries: a subclass must not use its parent's table.
    let table = class.getattr(intern!(py, "__dict__"))?;
    let Some(compiled) = resolve_compiled(py, class, &table, honour_custom_dump)? else {
        return Ok(None);
    };
    let mut names: SchemaWireNames = compiled.definitions[compiled.root]
        .fields
        .iter()
        .map(|field| {
            (
                field.name.clone(),
                if by_alias {
                    field.wire_name.clone()
                } else {
                    field.name.clone()
                },
            )
        })
        .collect();
    let computed = if with_computed {
        computed_names(&table, by_alias)?
    } else {
        Vec::new()
    };
    let extra = computed.len();
    names.extend(computed);
    Ok(Some((
        names,
        compiled.unset.clone_ref(py),
        extra,
        Arc::clone(&compiled.guides),
    )))
}

/// The class's own `(name, wire name)` pairs for its computed fields.
fn computed_names(table: &Bound<'_, PyAny>, by_alias: bool) -> PyResult<SchemaWireNames> {
    // Most classes have none: test first instead of building a `KeyError`.
    let key = intern!(table.py(), "__hinoki_computed__");
    if !table.contains(key)? {
        return Ok(Vec::new());
    }
    let entries = table.get_item(key)?;
    let mut names = Vec::new();
    for entry in entries.try_iter()? {
        let (name, wire_name): (String, String) = entry?.extract()?;
        let shown = if by_alias { wire_name } else { name.clone() };
        names.push((name, shown));
    }
    Ok(names)
}

/// Compile-check a `pattern=` at registration: the Rust `regex` syntax (linear
/// time, no look-around or back-references) matched against the whole value.
#[pyfunction]
pub fn check_pattern(source: &str) -> PyResult<()> {
    let verbose =
        Regex::new(r"\(\?[a-zA-Z]*x[a-zA-Z-]*[:)]").map_err(|e| compile_error(e.to_string()))?;
    if verbose.is_match(source) {
        return Err(compile_error("verbose mode (?x) is not supported"));
    }
    for banned in ["&&", "~~", "[[:"] {
        if source.contains(banned) {
            return Err(compile_error(format!(
                "{banned:?} (character-class set operations and POSIX classes) is not supported"
            )));
        }
    }
    Regex::new(&format!("\\A(?:{source})\\z"))
        .map(|_| ())
        .map_err(|e| compile_error(e.to_string()))
}

fn refers_outside<R: Rules>(node: &Node<R>, known: usize) -> bool {
    match &node.kind {
        Kind::Schema(index) => *index >= known,
        Kind::List(inner) | Kind::Map { value: inner, .. } => refers_outside(inner, known),
        Kind::Tuple(positions) | Kind::AnyOf(positions) => {
            positions.iter().any(|inner| refers_outside(inner, known))
        }
        Kind::TaggedUnion { variants, .. } => variants.iter().any(|(_, index)| *index >= known),
        _ => false,
    }
}
