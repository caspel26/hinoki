use std::borrow::Cow;
use std::cmp::Ordering;
use std::marker::PhantomData;

use pyo3::basic::CompareOp;
use pyo3::exceptions::PyUnicodeEncodeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};
use time::{Date, Month, PrimitiveDateTime, Time, UtcOffset};
use uuid::Uuid;

use super::compile::shown_choices;
use super::internet::NetKind;
use super::model::{
    Choice, Collection, Defer, Direct, Entries, LimitValue, NativeChoices, NativeLimit,
    NativeRules, Normalize, Number, PyClass, PyRules, Rules, Validated,
};
use super::scalars::{self, ClockTime, DecimalNumber, Duration};
use super::typeddecode;
use crate::request::{Json, JsonObject};

/// How the validator reads its input and builds its result. Every method
/// answers one question the walk asks, in the order the walk asks it, so a
/// view cannot change which problems are found or their order.
pub(super) trait View {
    type Rules: Rules;
    /// One input value.
    type Data;
    /// An input string.
    type Str;
    /// An input object.
    type Object;
    /// A string key of an input object.
    type Key;
    /// An input array.
    type List;
    /// One accepted value.
    type Out;
    /// An accepted array being built.
    type ListOut;
    /// The accepted fields of one object being built.
    type Record;
    type Error;

    fn is_null(&self, data: &Self::Data) -> bool;
    fn null(&self, data: &Self::Data) -> Self::Out;
    fn string(&self, data: &Self::Data) -> Option<Self::Str>;
    /// The text after `strip`, `lower` or `upper`.
    fn normalized(&self, text: Self::Str, form: Normalize) -> Result<Self::Str, Self::Error>;
    /// Length in code points.
    fn char_count(&self, text: &Self::Str) -> Result<usize, Self::Error>;
    fn text<'s>(&self, text: &'s Self::Str) -> Result<Cow<'s, str>, Self::Error>;
    fn string_out(&self, text: Self::Str) -> Self::Out;
    /// `None` for anything but an integer.
    fn integer(&self, data: &Self::Data) -> Result<Option<Self::Out>, Self::Error>;
    /// `None` for anything but an integer or a float; `Some(None)` when it
    /// has no `f64` value.
    fn number(&self, data: &Self::Data) -> Result<Option<Option<f64>>, Self::Error>;
    fn float(&self, value: f64) -> Self::Out;
    fn boolean(&self, data: &Self::Data) -> Option<Self::Out>;
    /// A canonical UUID string (`text` is its content).
    fn uuid(&self, data: &Self::Data, text: &str) -> Result<Self::Out, Self::Error>;
    /// An RFC 3339-shaped string: `None` when it is not a valid datetime,
    /// otherwise the value and whether it has no UTC offset.
    fn datetime(
        &self,
        data: &Self::Data,
        text: &str,
    ) -> Result<Option<(Self::Out, bool)>, Self::Error>;
    /// The text of a decimal input: a string as it is, an integer in
    /// decimal; `None` for anything else. An integer too long for Python to
    /// print gives a text past every digit limit.
    fn decimal_text(&self, data: &Self::Data) -> Result<Option<String>, Self::Error>;
    /// The value of a text the walk has validated as `wire`.
    fn wire(&self, wire: Wire, text: &str) -> Result<Self::Out, Self::Error>;
    fn list(&self, data: &Self::Data) -> Option<Self::List>;
    fn list_len(&self, list: &Self::List) -> usize;
    fn items(list: &Self::List) -> impl Iterator<Item = Self::Data>;
    fn new_list(&self) -> Self::ListOut;
    fn push(&self, out: &mut Self::ListOut, value: Self::Out) -> Result<(), Self::Error>;
    fn close_list(&self, out: Self::ListOut) -> Self::Out;
    fn object(&self, data: &Self::Data) -> Option<Self::Object>;
    fn non_string_keys(&self, object: &Self::Object) -> usize;
    fn string_keys(&self, object: &Self::Object) -> Vec<Self::Key>;
    /// The entries of an object whose keys are all strings, in order, each
    /// key as text or `None` when it is not valid Unicode (a lone surrogate).
    fn entries(&self, object: &Self::Object) -> Result<Entries<Self::Data>, Self::Error>;
    fn key_text<'k>(&self, key: &'k Self::Key) -> Result<Cow<'k, str>, Self::Error>;
    fn contains(&self, object: &Self::Object, key: &str) -> Result<bool, Self::Error>;
    fn get(&self, object: &Self::Object, key: &str) -> Result<Option<Self::Data>, Self::Error>;
    fn new_record(&self) -> Self::Record;
    fn set_field(
        &self,
        record: &mut Self::Record,
        name: &str,
        value: Self::Out,
    ) -> Result<(), Self::Error>;
    fn close_record(
        &self,
        class: &<Self::Rules as Rules>::Class,
        record: Self::Record,
        path: &str,
        construct: bool,
    ) -> Result<Self::Out, Self::Error>;
    /// The accepted entries of a `dict` field, as a plain mapping.
    fn close_map(&self, record: Self::Record) -> Self::Out;
    /// `Some(shown limit)` when `value` violates `limit` under `op`.
    fn limit_fails(
        &self,
        value: &Self::Out,
        limit: &<Self::Rules as Rules>::Limit,
        op: CompareOp,
    ) -> Result<Option<String>, Self::Error>;
    /// `Some(shown step)` when `value` is not a multiple of `step`.
    fn multiple_fails(
        &self,
        value: &Self::Out,
        step: &<Self::Rules as Rules>::Limit,
    ) -> Result<Option<String>, Self::Error>;
    fn is_choice(
        &self,
        value: &Self::Out,
        choices: &<Self::Rules as Rules>::Choices,
    ) -> Result<bool, Self::Error>;
    fn choices_shown(&self, choices: &<Self::Rules as Rules>::Choices) -> String;
    /// The member of an `Enum` class named by an accepted value.
    fn member(
        &self,
        class: &<Self::Rules as Rules>::Member,
        value: Self::Out,
    ) -> Result<Self::Out, Self::Error>;
    /// The container a checked array becomes (duplicates of a set collapse).
    fn collect(&self, collection: Collection, value: Self::Out) -> Result<Self::Out, Self::Error>;
    /// An `Any` field's value: the data as it was given.
    fn anything(&self, data: &Self::Data) -> Result<Self::Out, Self::Error>;
    fn pattern_shown(
        &self,
        pattern: &<Self::Rules as Rules>::Pattern,
    ) -> Result<String, Self::Error>;
}

/// A text the walk has checked as one of the scalar wire forms.
pub(super) enum Wire {
    Date(Date),
    Time(ClockTime),
    Duration(Duration),
    Decimal,
    Bytes(Vec<u8>),
    Net(NetKind),
    Path,
    Secret,
}

/// Reads Python objects and builds the Python result (`CompiledSchema.validate`).
pub(super) struct PyView<'a, 'py> {
    pub(super) py: Python<'py>,
    pub(super) uuid: &'a Py<PyAny>,
    pub(super) from_iso: &'a Py<PyAny>,
    /// The plan that completes a record whose omitted fields have defaults.
    pub(super) typed: Option<&'a typeddecode::Typed>,
}

impl<'py> View for PyView<'_, 'py> {
    type Rules = PyRules;
    type Data = Bound<'py, PyAny>;
    type Str = Bound<'py, PyString>;
    type Object = Bound<'py, PyDict>;
    type Key = Bound<'py, PyString>;
    type List = Bound<'py, PyList>;
    type Out = Bound<'py, PyAny>;
    type ListOut = Bound<'py, PyList>;
    type Record = Bound<'py, PyDict>;
    type Error = PyErr;

    fn is_null(&self, data: &Self::Data) -> bool {
        data.is_none()
    }

    fn null(&self, data: &Self::Data) -> Self::Out {
        data.clone()
    }

    fn string(&self, data: &Self::Data) -> Option<Self::Str> {
        data.cast::<PyString>().ok().cloned()
    }

    fn normalized(&self, text: Self::Str, form: Normalize) -> PyResult<Self::Str> {
        let changed = match form.apply(&text.to_cow()?) {
            Cow::Borrowed(_) => return Ok(text),
            Cow::Owned(changed) => changed,
        };
        Ok(PyString::new(text.py(), &changed))
    }

    fn char_count(&self, text: &Self::Str) -> PyResult<usize> {
        text.len()
    }

    fn text<'s>(&self, text: &'s Self::Str) -> PyResult<Cow<'s, str>> {
        text.to_cow()
    }

    fn string_out(&self, text: Self::Str) -> Self::Out {
        text.into_any()
    }

    fn integer(&self, data: &Self::Data) -> PyResult<Option<Self::Out>> {
        Ok(data.is_exact_instance_of::<PyInt>().then(|| data.clone()))
    }

    fn number(&self, data: &Self::Data) -> PyResult<Option<Option<f64>>> {
        if !(data.is_exact_instance_of::<PyInt>() || data.is_exact_instance_of::<PyFloat>()) {
            return Ok(None);
        }
        Ok(Some(data.extract::<f64>().ok()))
    }

    fn float(&self, value: f64) -> Self::Out {
        PyFloat::new(self.py, value).into_any()
    }

    fn boolean(&self, data: &Self::Data) -> Option<Self::Out> {
        data.is_exact_instance_of::<PyBool>().then(|| data.clone())
    }

    fn uuid(&self, data: &Self::Data, _text: &str) -> PyResult<Self::Out> {
        self.uuid.bind(self.py).call1((data,))
    }

    fn datetime(&self, data: &Self::Data, _text: &str) -> PyResult<Option<(Self::Out, bool)>> {
        let parsed = match self.from_iso.bind(self.py).call1((data,)) {
            Ok(value) => value,
            Err(err) if err.is_instance_of::<pyo3::exceptions::PyValueError>(self.py) => {
                return Ok(None);
            }
            Err(err) => return Err(err),
        };
        let naive = parsed.getattr("tzinfo")?.is_none();
        Ok(Some((parsed, naive)))
    }

    fn decimal_text(&self, data: &Self::Data) -> PyResult<Option<String>> {
        if let Ok(text) = data.cast::<PyString>() {
            return Ok(Some(text.to_cow()?.into_owned()));
        }
        if !data.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
        match data.str() {
            Ok(text) => Ok(Some(text.to_string())),
            Err(err) if err.is_instance_of::<pyo3::exceptions::PyValueError>(self.py) => {
                Ok(Some("9".repeat(scalars::DECIMAL_DIGITS_CEILING + 1)))
            }
            Err(err) => Err(err),
        }
    }

    fn wire(&self, wire: Wire, text: &str) -> PyResult<Self::Out> {
        let py = self.py;
        match wire {
            Wire::Date(_) => crate::pytypes::date_class(py)?.call_method1("fromisoformat", (text,)),
            Wire::Time(_) => crate::pytypes::time_class(py)?.call_method1("fromisoformat", (text,)),
            Wire::Duration(span) => {
                crate::pytypes::timedelta_class(py)?.call1((span.days, span.seconds, span.micros))
            }
            Wire::Decimal => crate::pytypes::decimal_class(py)?.call1((text,)),
            Wire::Bytes(raw) => Ok(PyBytes::new(py, &raw).into_any()),
            Wire::Net(kind) => py
                .import("ipaddress")?
                .getattr(kind.class_name())?
                .call1((text,)),
            Wire::Path => crate::pytypes::path_class(py)?.call1((text,)),
            Wire::Secret => crate::pytypes::secret_class(py)?.call1((text,)),
        }
    }

    fn list(&self, data: &Self::Data) -> Option<Self::List> {
        data.cast::<PyList>().ok().cloned()
    }

    fn list_len(&self, list: &Self::List) -> usize {
        list.len()
    }

    fn items(list: &Self::List) -> impl Iterator<Item = Self::Data> {
        list.iter()
    }

    fn new_list(&self) -> Self::ListOut {
        PyList::empty(self.py)
    }

    fn push(&self, out: &mut Self::ListOut, value: Self::Out) -> PyResult<()> {
        out.append(value)
    }

    fn close_list(&self, out: Self::ListOut) -> Self::Out {
        out.into_any()
    }

    fn object(&self, data: &Self::Data) -> Option<Self::Object> {
        data.cast::<PyDict>().ok().cloned()
    }

    fn non_string_keys(&self, object: &Self::Object) -> usize {
        object
            .keys()
            .iter()
            .filter(|key| key.cast::<PyString>().is_err())
            .count()
    }

    fn string_keys(&self, object: &Self::Object) -> Vec<Self::Key> {
        object
            .keys()
            .iter()
            .filter_map(|key| key.cast_into::<PyString>().ok())
            .collect()
    }

    fn key_text<'k>(&self, key: &'k Self::Key) -> PyResult<Cow<'k, str>> {
        key.to_cow()
    }

    fn entries(&self, object: &Self::Object) -> PyResult<Entries<Self::Data>> {
        let mut found = Vec::with_capacity(object.len());
        for (key, value) in object.iter() {
            let Ok(text) = key.cast::<PyString>() else {
                continue;
            };
            let text = match text.to_cow() {
                Ok(text) => Some(text.into_owned()),
                Err(err) if err.is_instance_of::<PyUnicodeEncodeError>(self.py) => None,
                Err(err) => return Err(err),
            };
            found.push((text, value));
        }
        Ok(found)
    }

    fn contains(&self, object: &Self::Object, key: &str) -> PyResult<bool> {
        object.contains(key)
    }

    fn get(&self, object: &Self::Object, key: &str) -> PyResult<Option<Self::Data>> {
        object.get_item(key)
    }

    fn new_record(&self) -> Self::Record {
        PyDict::new(self.py)
    }

    fn set_field(&self, record: &mut Self::Record, name: &str, value: Self::Out) -> PyResult<()> {
        record.set_item(name, value)
    }

    fn close_record(
        &self,
        class: &PyClass,
        record: Self::Record,
        path: &str,
        construct: bool,
    ) -> PyResult<Self::Out> {
        if !construct {
            return Ok(record.into_any());
        }
        let class_object = class.object.bind(self.py);
        if let Some(direct) = class.direct.as_ref() {
            if record.len() == direct.fields {
                return build_direct(self.py, class_object, direct, &record, None);
            }
            if let Some(typed) = self.typed {
                if let Some(done) = typeddecode::complete(typed, direct.index, &record)? {
                    return build_direct(self.py, class_object, direct, &record, done.supplied);
                }
            }
        }
        class_object.call_method1("__hinoki_construct_validated__", (record, path))
    }

    fn close_map(&self, record: Self::Record) -> Self::Out {
        record.into_any()
    }

    fn limit_fails(
        &self,
        value: &Self::Out,
        limit: &Py<PyAny>,
        op: CompareOp,
    ) -> PyResult<Option<String>> {
        let limit = limit.bind(self.py);
        if value.rich_compare(limit, op)?.is_truthy()? {
            return Ok(None);
        }
        Ok(Some(limit.str()?.to_string()))
    }

    fn multiple_fails(&self, value: &Self::Out, step: &Py<PyAny>) -> PyResult<Option<String>> {
        let step = step.bind(self.py);
        // Exact fractions: `Decimal` arithmetic would round past its precision.
        let ratio =
            |number: &Bound<'py, PyAny>| -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
                number.call_method0("as_integer_ratio")?.extract()
            };
        let (value_n, value_d) = ratio(value)?;
        let (step_n, step_d) = ratio(step)?;
        let left = value_n.mul(&step_d)?;
        let right = step_n.mul(&value_d)?;
        if left.rem(&right)?.is_truthy()? {
            return Ok(Some(step.str()?.to_string()));
        }
        Ok(None)
    }

    fn is_choice(&self, value: &Self::Out, choices: &Vec<Py<PyAny>>) -> PyResult<bool> {
        for choice in choices {
            if value.eq(choice.bind(self.py))? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn choices_shown(&self, choices: &Vec<Py<PyAny>>) -> String {
        shown_choices(self.py, choices)
    }

    fn member(&self, class: &Py<PyAny>, value: Self::Out) -> PyResult<Self::Out> {
        class.bind(self.py).call1((value,))
    }

    fn collect(&self, collection: Collection, value: Self::Out) -> PyResult<Self::Out> {
        let builtin = match collection {
            Collection::Set => self.py.get_type::<pyo3::types::PySet>().into_any(),
            Collection::Frozenset => self.py.get_type::<pyo3::types::PyFrozenSet>().into_any(),
            Collection::Tuple => self.py.get_type::<pyo3::types::PyTuple>().into_any(),
        };
        builtin.call1((value,))
    }

    fn anything(&self, data: &Self::Data) -> PyResult<Self::Out> {
        Ok(data.clone())
    }

    fn pattern_shown(&self, pattern: &Py<PyAny>) -> PyResult<String> {
        Ok(pattern.bind(self.py).repr()?.to_string())
    }
}

/// Reads the GIL-free [`Json`] tree and builds [`Validated`] values.
pub(super) struct JsonView<'v> {
    pub(super) input: PhantomData<&'v Json>,
}

impl Number {
    fn of(value: &Validated) -> Option<Number> {
        match value {
            Validated::Int(int) => Some(Number::Int(*int)),
            Validated::Float(float) => Some(Number::Float(*float)),
            Validated::Bool(flag) => Some(Number::Int(i64::from(*flag))),
            _ => None,
        }
    }
}

pub(super) const TWO_POW_63: f64 = 9_223_372_036_854_775_808.0;

/// Python's exact comparison of an `int` with a `float` (no rounding).
fn compare_int_float(int: i64, float: f64) -> Option<Ordering> {
    if float.is_nan() {
        return None;
    }
    if float >= TWO_POW_63 {
        return Some(Ordering::Less);
    }
    if float < -TWO_POW_63 {
        return Some(Ordering::Greater);
    }
    let whole = float.trunc();
    // In range and integral, so the cast is exact.
    #[expect(
        clippy::cast_possible_truncation,
        reason = "whole is integral and in i64 range"
    )]
    let whole_int = whole as i64;
    match int.cmp(&whole_int) {
        Ordering::Equal => 0.0_f64.partial_cmp(&(float - whole)),
        other => Some(other),
    }
}

/// Python's ordering of two numbers; `None` when unordered (NaN).
pub(super) fn compare_numbers(left: Number, right: Number) -> Option<Ordering> {
    match (left, right) {
        (Number::Int(a), Number::Int(b)) => Some(a.cmp(&b)),
        (Number::Float(a), Number::Float(b)) => a.partial_cmp(&b),
        (Number::Int(a), Number::Float(b)) => compare_int_float(a, b),
        (Number::Float(a), Number::Int(b)) => compare_int_float(b, a).map(Ordering::reverse),
    }
}

pub(super) fn holds(op: CompareOp, order: Option<Ordering>) -> bool {
    let Some(order) = order else {
        return matches!(op, CompareOp::Ne);
    };
    match op {
        CompareOp::Ge => order != Ordering::Less,
        CompareOp::Le => order != Ordering::Greater,
        CompareOp::Gt => order == Ordering::Greater,
        CompareOp::Lt => order == Ordering::Less,
        CompareOp::Eq => order == Ordering::Equal,
        CompareOp::Ne => order != Ordering::Equal,
    }
}

fn equals_choice(value: &Validated, choice: &Choice) -> bool {
    match (Number::of(value), choice) {
        (Some(number), Choice::Number(expected)) => {
            holds(CompareOp::Eq, compare_numbers(number, *expected))
        }
        (None, Choice::Str(expected)) => matches!(value, Validated::Str(text) if text == expected),
        _ => false,
    }
}

pub(super) const UNCERTAIN_DATETIME: Defer = Defer("datetime Python may read differently");

fn digits_value(bytes: &[u8]) -> u32 {
    bytes
        .iter()
        .fold(0, |total, digit| total * 10 + u32::from(digit - b'0'))
}

/// The microseconds of an optional `.fraction` at `at` (1 to 6 digits; more
/// are left to Python) and where the text continues.
fn fraction_micros(bytes: &[u8], at: usize) -> Result<(u32, usize), Defer> {
    if bytes.get(at) != Some(&b'.') {
        return Ok((0, at));
    }
    let start = at + 1;
    let end = start
        + bytes[start..]
            .iter()
            .take_while(|byte| byte.is_ascii_digit())
            .count();
    let count = end - start;
    if count == 0 || count > 6 {
        return Err(UNCERTAIN_DATETIME);
    }
    let scale = 10_u32.pow(u32::try_from(6 - count).map_err(|_| UNCERTAIN_DATETIME)?);
    Ok((digits_value(&bytes[start..end]) * scale, end))
}

/// The UTC offset in seconds of `rest` (`Z` or `+hh:mm`/`-hh:mm` within a
/// day), `None` when there is none.
fn offset_seconds(rest: &[u8]) -> Result<Option<i32>, Defer> {
    let (sign, digits) = match rest {
        [] => return Ok(None),
        [b'Z'] => return Ok(Some(0)),
        [b'+', digits @ ..] => (1, digits),
        [b'-', digits @ ..] => (-1, digits),
        _ => return Err(UNCERTAIN_DATETIME),
    };
    let hours = digits_value(&digits[..2]);
    let minutes = digits_value(&digits[3..5]);
    if hours > 23 || minutes > 59 {
        return Err(UNCERTAIN_DATETIME);
    }
    let seconds = i32::try_from(hours * 3600 + minutes * 60).map_err(|_| UNCERTAIN_DATETIME)?;
    Ok(Some(sign * seconds))
}

/// An RFC 3339-shaped string (see [`is_rfc3339`]) as `datetime.fromisoformat`
/// reads it, for the inputs where every supported Python version agrees:
/// upper-case `T`/`Z`, real calendar dates from year 1, seconds below 60, at
/// most six fraction digits, offsets within a day. Anything else is
/// [`Defer`]red, never guessed.
fn native_datetime(text: &str) -> Result<(Validated, bool), Defer> {
    let bytes = text.as_bytes();
    if bytes.get(10) != Some(&b'T') {
        return Err(UNCERTAIN_DATETIME);
    }
    let field = |from: usize, to: usize| digits_value(&bytes[from..to]);
    let year = i32::try_from(field(0, 4)).map_err(|_| UNCERTAIN_DATETIME)?;
    let month = u8::try_from(field(5, 7)).map_err(|_| UNCERTAIN_DATETIME)?;
    let day = u8::try_from(field(8, 10)).map_err(|_| UNCERTAIN_DATETIME)?;
    let (hour, minute, second) = (field(11, 13), field(14, 16), field(17, 19));
    if year == 0 || hour > 23 || minute > 59 || second > 59 {
        return Err(UNCERTAIN_DATETIME);
    }
    let (micros, at) = fraction_micros(bytes, 19)?;
    let offset = offset_seconds(&bytes[at..])?;
    let month = Month::try_from(month).map_err(|_| UNCERTAIN_DATETIME)?;
    let date = Date::from_calendar_date(year, month, day).map_err(|_| UNCERTAIN_DATETIME)?;
    let clock = |value: u32| u8::try_from(value).map_err(|_| UNCERTAIN_DATETIME);
    let time = Time::from_hms_micro(clock(hour)?, clock(minute)?, clock(second)?, micros)
        .map_err(|_| UNCERTAIN_DATETIME)?;
    let local = PrimitiveDateTime::new(date, time);
    let Some(seconds) = offset else {
        return Ok((Validated::NaiveDatetime(local), true));
    };
    let offset = UtcOffset::from_whole_seconds(seconds).map_err(|_| UNCERTAIN_DATETIME)?;
    Ok((Validated::Datetime(local.assume_offset(offset)), false))
}

const BIG_INTEGER: Defer = Defer("integer outside i64");

impl<'v> View for JsonView<'v> {
    type Rules = NativeRules;
    type Data = &'v Json;
    type Str = Cow<'v, str>;
    type Object = &'v JsonObject;
    type Key = &'v str;
    type List = &'v [Json];
    type Out = Validated;
    type ListOut = Vec<Validated>;
    type Record = Vec<(String, Validated)>;
    type Error = Defer;

    fn is_null(&self, data: &Self::Data) -> bool {
        matches!(data, Json::Null)
    }

    fn null(&self, _data: &Self::Data) -> Validated {
        Validated::Null
    }

    fn string(&self, data: &Self::Data) -> Option<Cow<'v, str>> {
        match data {
            Json::Str(text) => Some(Cow::Borrowed(text.as_str())),
            _ => None,
        }
    }

    fn normalized(&self, text: Cow<'v, str>, form: Normalize) -> Result<Cow<'v, str>, Defer> {
        Ok(match form.apply(&text) {
            Cow::Borrowed(_) => text,
            Cow::Owned(changed) => Cow::Owned(changed),
        })
    }

    fn char_count(&self, text: &Cow<'v, str>) -> Result<usize, Defer> {
        Ok(text.chars().count())
    }

    fn text<'s>(&self, text: &'s Cow<'v, str>) -> Result<Cow<'s, str>, Defer> {
        Ok(Cow::Borrowed(text.as_ref()))
    }

    fn string_out(&self, text: Cow<'v, str>) -> Validated {
        Validated::Str(text.into_owned())
    }

    fn integer(&self, data: &Self::Data) -> Result<Option<Validated>, Defer> {
        match data {
            Json::Int(int) => Ok(Some(Validated::Int(*int))),
            Json::BigInt(_) => Err(BIG_INTEGER),
            _ => Ok(None),
        }
    }

    fn number(&self, data: &Self::Data) -> Result<Option<Option<f64>>, Defer> {
        match data {
            // Round to nearest, ties to even: Python's int-to-float conversion.
            #[expect(
                clippy::cast_precision_loss,
                reason = "Python rounds int to float the same way"
            )]
            Json::Int(int) => Ok(Some(Some(*int as f64))),
            Json::Float(float) => Ok(Some(Some(*float))),
            Json::BigInt(_) => Err(BIG_INTEGER),
            _ => Ok(None),
        }
    }

    fn float(&self, value: f64) -> Validated {
        Validated::Float(value)
    }

    fn boolean(&self, data: &Self::Data) -> Option<Validated> {
        match data {
            Json::Bool(flag) => Some(Validated::Bool(*flag)),
            _ => None,
        }
    }

    fn uuid(&self, _data: &Self::Data, text: &str) -> Result<Validated, Defer> {
        Uuid::parse_str(text)
            .map(Validated::Uuid)
            .map_err(|_| Defer("uuid"))
    }

    fn datetime(&self, _data: &Self::Data, text: &str) -> Result<Option<(Validated, bool)>, Defer> {
        native_datetime(text).map(Some)
    }

    fn decimal_text(&self, data: &Self::Data) -> Result<Option<String>, Defer> {
        match data {
            Json::Str(text) => Ok(Some(text.clone())),
            Json::Int(int) => Ok(Some(int.to_string())),
            Json::BigInt(_) => Err(BIG_INTEGER),
            _ => Ok(None),
        }
    }

    fn wire(&self, wire: Wire, text: &str) -> Result<Validated, Defer> {
        Ok(match wire {
            Wire::Date(day) => Validated::Date(day),
            Wire::Time(clock) => Validated::Time(clock),
            Wire::Duration(span) => Validated::Duration(span),
            Wire::Decimal => Validated::Decimal(text.to_owned()),
            Wire::Bytes(raw) => Validated::Bytes(raw),
            Wire::Net(kind) => Validated::Net(kind, text.to_owned()),
            Wire::Path | Wire::Secret => return Err(Defer("path and secret values")),
        })
    }

    fn list(&self, data: &Self::Data) -> Option<&'v [Json]> {
        match data {
            Json::List(items) => Some(items.as_slice()),
            _ => None,
        }
    }

    fn list_len(&self, list: &&'v [Json]) -> usize {
        list.len()
    }

    fn items(list: &&'v [Json]) -> impl Iterator<Item = &'v Json> {
        list.iter()
    }

    fn new_list(&self) -> Vec<Validated> {
        Vec::new()
    }

    fn push(&self, out: &mut Vec<Validated>, value: Validated) -> Result<(), Defer> {
        out.push(value);
        Ok(())
    }

    fn close_list(&self, out: Vec<Validated>) -> Validated {
        Validated::List(out)
    }

    fn object(&self, data: &Self::Data) -> Option<&'v JsonObject> {
        match data {
            Json::Object(object) => Some(object),
            _ => None,
        }
    }

    fn non_string_keys(&self, _object: &&'v JsonObject) -> usize {
        0
    }

    fn string_keys(&self, object: &&'v JsonObject) -> Vec<&'v str> {
        object.keys().collect()
    }

    fn key_text<'k>(&self, key: &'k &'v str) -> Result<Cow<'k, str>, Defer> {
        Ok(Cow::Borrowed(key))
    }

    fn entries(&self, object: &&'v JsonObject) -> Result<Entries<&'v Json>, Defer> {
        Ok(object
            .iter()
            .map(|(key, value)| (Some(key.to_owned()), value))
            .collect())
    }

    fn contains(&self, object: &&'v JsonObject, key: &str) -> Result<bool, Defer> {
        Ok(object.contains(key))
    }

    fn get(&self, object: &&'v JsonObject, key: &str) -> Result<Option<&'v Json>, Defer> {
        Ok(object.get(key))
    }

    fn new_record(&self) -> Vec<(String, Validated)> {
        Vec::new()
    }

    fn set_field(
        &self,
        record: &mut Vec<(String, Validated)>,
        name: &str,
        value: Validated,
    ) -> Result<(), Defer> {
        record.push((name.to_owned(), value));
        Ok(())
    }

    fn close_record(
        &self,
        _class: &(),
        record: Vec<(String, Validated)>,
        _path: &str,
        _construct: bool,
    ) -> Result<Validated, Defer> {
        Ok(Validated::Object(record))
    }

    fn close_map(&self, record: Vec<(String, Validated)>) -> Validated {
        Validated::Object(record)
    }

    fn limit_fails(
        &self,
        value: &Validated,
        limit: &NativeLimit,
        op: CompareOp,
    ) -> Result<Option<String>, Defer> {
        let order = match (value, &limit.value) {
            (Validated::Decimal(text), LimitValue::Decimal(bound)) => {
                let number = DecimalNumber::parse(text).ok_or(Defer("decimal bound"))?;
                Some(number.cmp(bound))
            }
            (_, LimitValue::Number(bound)) => {
                let number = Number::of(value).ok_or(Defer("bound on a non-number"))?;
                compare_numbers(number, *bound)
            }
            _ => return Err(Defer("bound on a non-number")),
        };
        if holds(op, order) {
            return Ok(None);
        }
        Ok(Some(limit.shown.clone()))
    }

    fn multiple_fails(
        &self,
        value: &Validated,
        step: &NativeLimit,
    ) -> Result<Option<String>, Defer> {
        let multiple = match (value, &step.value) {
            (Validated::Int(int), LimitValue::Number(Number::Int(step))) => {
                int.checked_rem(*step) == Some(0)
            }
            (Validated::Decimal(text), LimitValue::Decimal(step)) => DecimalNumber::parse(text)
                .and_then(|number| number.is_multiple_of(step))
                .ok_or(Defer("multiple_of"))?,
            _ => return Err(Defer("multiple_of")),
        };
        Ok((!multiple).then(|| step.shown.clone()))
    }

    fn is_choice(&self, value: &Validated, choices: &NativeChoices) -> Result<bool, Defer> {
        Ok(choices
            .values
            .iter()
            .any(|choice| equals_choice(value, choice)))
    }

    fn choices_shown(&self, choices: &NativeChoices) -> String {
        choices.shown.clone()
    }

    fn member(&self, _class: &(), value: Validated) -> Result<Validated, Defer> {
        Ok(value)
    }

    fn collect(&self, _collection: Collection, _value: Validated) -> Result<Validated, Defer> {
        Err(Defer("collection"))
    }

    fn anything(&self, _data: &&'v Json) -> Result<Validated, Defer> {
        Err(Defer("any"))
    }

    fn pattern_shown(&self, pattern: &String) -> Result<String, Defer> {
        Ok(pattern.clone())
    }
}

/// `object.__new__(class)` filled from `record`, as the generated constructor
/// would, without the Python round trip.
fn build_direct<'py>(
    py: Python<'py>,
    class: &Bound<'py, PyAny>,
    direct: &Direct,
    record: &Bound<'py, PyDict>,
    given: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let instance = py.get_type::<PyAny>().call_method1("__new__", (class,))?;
    for (key, value) in record.iter() {
        typeddecode::assign(&instance, key.cast::<PyString>()?, &value)?;
    }
    if let Some(supplied) = given.or_else(|| direct.supplied.as_ref().map(|s| s.bind(py).clone())) {
        typeddecode::assign(
            &instance,
            &PyString::intern(py, "__hinoki_supplied_fields__"),
            &supplied,
        )?;
    }
    Ok(instance)
}
