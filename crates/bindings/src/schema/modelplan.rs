use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyString};

use super::typeddecode;

/// How one model attribute converts for a schema field: a nested model becomes
/// `schema.from_model(value)`, a list or tuple maps its items through `item`
/// into a new list, a dict (for a `dict` field) its values into a new dict,
/// anything else passes through unchanged.
struct Conversion {
    schema: Option<Py<PyAny>>,
    item: Option<Box<Conversion>>,
    mapping: bool,
}

fn parse_conversion(raw: &Bound<'_, PyAny>) -> PyResult<Conversion> {
    let (schema, item, mapping): (Option<Py<PyAny>>, Option<Bound<'_, PyAny>>, bool) =
        raw.extract()?;
    Ok(Conversion {
        schema,
        item: match item {
            Some(inner) => Some(Box::new(parse_conversion(&inner)?)),
            None => None,
        },
        mapping,
    })
}

struct PlanField {
    key: Py<pyo3::types::PyString>,
    name: String,
    required: bool,
    conversion: Conversion,
}

/// The fields of a schema as `from_model` reads them, prepared once per class.
#[pyclass(name = "ModelPlan", module = "hinoki.core")]
pub struct ModelPlan {
    class: Py<PyAny>,
    fields: Vec<PlanField>,
    direct: bool,
}

#[pymethods]
impl ModelPlan {
    /// `fields` is `[(name, required, (schema, item)), ...]`; `direct` says the
    /// class uses the generated constructor (no `__init__`/`__post_init__` of its
    /// own), so instances may be built without calling it.
    #[new]
    #[pyo3(signature = (class, fields, direct=true))]
    fn new(
        class: Py<PyAny>,
        fields: Vec<(String, bool, Bound<'_, PyAny>)>,
        direct: bool,
    ) -> PyResult<Self> {
        let mut planned = Vec::with_capacity(fields.len());
        for (name, required, conversion) in fields {
            planned.push(PlanField {
                key: pyo3::types::PyString::new(conversion.py(), &name).unbind(),
                name,
                required,
                conversion: parse_conversion(&conversion)?,
            });
        }
        Ok(Self {
            class,
            fields: planned,
            direct,
        })
    }

    /// `Schema.from_model` in Rust: read each planned field from `row` (`extra`
    /// wins), convert nested models and build the instance. An unloaded relation
    /// still raises `RelationNotLoaded`; a missing required attribute raises
    /// `AttributeError`.
    #[pyo3(signature = (row, extra=None))]
    fn build<'py>(
        &self,
        py: Python<'py>,
        row: &Bound<'py, PyAny>,
        extra: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let class = self.class.bind(py);
        let mut pairs: Vec<(&Bound<'py, PyString>, Bound<'py, PyAny>)> =
            Vec::with_capacity(self.fields.len());
        for field in &self.fields {
            let key = field.key.bind(py);
            if let Some(found) = extra.and_then(|g| g.get_item(key).transpose()) {
                pairs.push((key, found?));
                continue;
            }
            match row.getattr(key) {
                Ok(value) => pairs.push((key, convert_attribute(&value, &field.conversion)?)),
                Err(error) => self.missing(py, row, field, error)?,
            }
        }
        if self.direct && pairs.len() == self.fields.len() {
            // Every field is present: build the instance directly, skipping both
            // the intermediate dict and the dataclass constructor.
            let instance = crate::pytypes::object_new(py)?.call1((class,))?;
            for (key, value) in &pairs {
                typeddecode::assign(&instance, key, value)?;
            }
            typeddecode::assign(
                &instance,
                pyo3::intern!(py, "__hinoki_supplied_fields__"),
                &PyBool::new(py, true).to_owned().into_any(),
            )?;
            return Ok(instance);
        }
        let values = PyDict::new(py);
        for (key, value) in pairs {
            values.set_item(key, value)?;
        }
        class.call_method1(
            pyo3::intern!(py, "__hinoki_construct_from_model__"),
            (values,),
        )
    }
}

impl ModelPlan {
    /// An attribute was not there: propagate a loading mistake or a real error,
    /// raise for a required field, skip an optional one.
    fn missing(
        &self,
        py: Python<'_>,
        row: &Bound<'_, PyAny>,
        field: &PlanField,
        error: PyErr,
    ) -> PyResult<()> {
        let unloaded = crate::pytypes::relation_not_loaded_class(py)?;
        if error.value(py).is_instance(unloaded)?
            || !error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py)
        {
            return Err(error); // a loading mistake or an unrelated failure
        }
        if field.required {
            let schema_name: String = self.class.bind(py).getattr("__name__")?.extract()?;
            let row_name: String = row.get_type().name()?.extract()?;
            let name = &field.name;
            return Err(pyo3::exceptions::PyAttributeError::new_err(format!(
                "{schema_name}.{name}: {row_name} has no attribute '{name}'; pass it as {name}=..."
            )));
        }
        Ok(())
    }
}

fn convert_attribute<'py>(
    value: &Bound<'py, PyAny>,
    conversion: &Conversion,
) -> PyResult<Bound<'py, PyAny>> {
    if value.is_none() {
        return Ok(value.clone());
    }
    if let Some(class) = &conversion.schema {
        if value.get_type().hasattr("__table__")? {
            return class.bind(value.py()).call_method1("from_model", (value,));
        }
    }
    if let Some(inner) = &conversion.item {
        if conversion.mapping {
            return convert_mapping(value, inner);
        }
        if value.is_instance_of::<PyList>() || value.is_instance_of::<pyo3::types::PyTuple>() {
            let out = PyList::empty(value.py());
            for element in value.try_iter()? {
                out.append(convert_attribute(&element?, inner)?)?;
            }
            return Ok(out.into_any());
        }
    }
    Ok(value.clone())
}

/// A dict attribute copied with its values converted; anything else is left for
/// validation to report.
fn convert_mapping<'py>(
    value: &Bound<'py, PyAny>,
    inner: &Conversion,
) -> PyResult<Bound<'py, PyAny>> {
    let Ok(dict) = value.cast::<PyDict>() else {
        return Ok(value.clone());
    };
    let out = PyDict::new(value.py());
    for (key, element) in dict.iter() {
        out.set_item(key, convert_attribute(&element, inner)?)?;
    }
    Ok(out.into_any())
}
