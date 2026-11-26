//! Typed body decode: the bytes of a JSON body straight to schema instances.
//!
//! One pass over the bytes, guided by the schema: each value is checked as it
//! is read and becomes its Python object at once, so no `dict`/`list` tree of
//! the raw document, no GIL-free copy of it and no validated copy exist.
//!
//! The decode is optimistic. It accepts only documents that the ordinary
//! `decode_json` plus `Schema.validate` accept with the same result, and
//! returns `None` for everything else (a syntax error, a problem, a duplicate
//! or unknown key it cannot treat identically, an integer outside `i64`, a
//! constraint outside the subset below, a refusal by a constructor). The caller
//! then runs the ordinary path on the same bytes, which reports exactly what
//! it always reported. Accepting a document the ordinary path refuses is the
//! one thing this module must never do, so every rule below is the stricter of
//! the two grammars.

mod nativeinit;
mod nativemethods;

pub use nativeinit::NativeInit;
pub(crate) use nativeinit::{complete, from_record, replaced};
pub use nativemethods::{NativeMethod, Pinned};

use std::borrow::Cow;
use std::collections::HashMap;

use pyo3::basic::CompareOp;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple, PyType};

use super::{
    compare_numbers, holds, Definition, Kind, NativeLimit, NativeRules, NativeSchema, Node, Number,
    PyRules,
};

/// A nesting level the decode refuses (the ordinary parser's own bound is 128).
const MAX_NESTING: usize = 100;
/// More fields than this are looked up by hash instead of by scan.
const SCAN_LIMIT: usize = 12;

/// Why the typed result is not available.
enum Stop {
    /// Run the ordinary path on the same bytes.
    Fallback,
    /// A Python exception that must reach the caller (boxed, so that every
    /// result of the reader stays small enough to be returned in registers).
    Raise(Box<PyErr>),
}

impl From<PyErr> for Stop {
    fn from(err: PyErr) -> Self {
        Stop::Raise(Box::new(err))
    }
}

type Step<T> = Result<T, Stop>;

/// The bounds a numeric field declares.
#[derive(Default)]
struct Bounds {
    ge: Option<Number>,
    le: Option<Number>,
    gt: Option<Number>,
    lt: Option<Number>,
}

impl Bounds {
    fn holds(&self, value: Number) -> bool {
        if self.ge.is_none() && self.le.is_none() && self.gt.is_none() && self.lt.is_none() {
            return true;
        }
        let tests = [
            (self.ge, CompareOp::Ge),
            (self.le, CompareOp::Le),
            (self.gt, CompareOp::Gt),
            (self.lt, CompareOp::Lt),
        ];
        tests
            .into_iter()
            .all(|(bound, op)| bound.is_none_or(|b| holds(op, compare_numbers(value, b))))
    }
}

enum Shape {
    Str {
        min: Option<i64>,
        max: Option<i64>,
    },
    Int(Bounds),
    Float(Bounds),
    Bool,
    Datetime,
    List {
        min: Option<i64>,
        max: Option<i64>,
        item: Box<Plan>,
    },
    Schema(usize),
}

/// What a value must be, and so how its bytes are read.
struct Plan {
    nullable: bool,
    shape: Shape,
}

struct PlanField {
    /// The key the field is read from (its wire name).
    key: Box<[u8]>,
    /// `"key":` as written in compact JSON, matched in one comparison.
    pattern: Box<[u8]>,
    name: Py<PyString>,
    /// `key` as a `str`, to look it up in a mapping.
    wire: Py<PyString>,
    required: bool,
    plan: Plan,
}

/// Where the instances of a class keep their fields: the byte offset of each
/// field's slot, in declaration order, and of the supplied-fields slot.
struct Layout {
    offsets: Vec<isize>,
    supplied: Option<isize>,
    /// One bit per field: the required ones, and all of them.
    required: u128,
    full: u128,
}

struct PlanClass {
    class: Py<PyAny>,
    fields: Vec<PlanField>,
    /// The slots every field lives in, or `None` when some field is kept
    /// elsewhere (a class that was derived from a model, say): then the
    /// attributes are assigned by name.
    layout: Option<Layout>,
    forbid_extra: bool,
    /// The number of fields a record needs to be built directly.
    direct_fields: usize,
    supplied: Option<Py<PyAny>>,
    /// The Python names of aliased fields: sent beside their alias they are
    /// a `duplicate_field` problem, so a body holding one is not typed.
    renamed: Vec<Box<[u8]>>,
    /// Field positions by key, when there are too many to scan.
    index: Option<HashMap<Box<[u8]>, usize>>,
    /// What each field takes when it is omitted, once the whole table is known.
    omitted: Vec<nativeinit::Omitted>,
}

/// Everything the decode needs about one compiled schema.
pub(super) struct Typed {
    classes: Vec<PlanClass>,
    root: usize,
    /// `object.__new__`, looked up once.
    allocate: Py<PyAny>,
    /// The attribute that records the supplied fields.
    supplied: Py<PyString>,
}

/// The decode plan of a schema, or `None` when some field is outside the subset:
/// text, integers, floats, booleans, datetimes, lists and nested schemas, with
/// length and numeric bounds, every class built directly.
pub(super) fn typed(
    py: Python<'_>,
    definitions: &[Definition<PyRules>],
    native: &NativeSchema,
    root: usize,
) -> Option<Typed> {
    let mut classes = Vec::with_capacity(definitions.len());
    for (definition, lowered) in definitions.iter().zip(&native.definitions) {
        classes.push(class_plan(py, definition, lowered)?);
    }
    let allocate = py.get_type::<PyAny>().getattr("__new__").ok()?.unbind();
    let mut typed = Typed {
        classes,
        root,
        allocate,
        supplied: PyString::intern(py, "__hinoki_supplied_fields__").unbind(),
    };
    for (at, definition) in definitions.iter().enumerate() {
        let defaults = definition
            .class
            .direct
            .as_ref()
            .and_then(|direct| direct.defaults.as_ref());
        if let Some((values, factories)) = defaults {
            let omitted = nativeinit::omitted_of(
                &typed,
                &typed.classes[at],
                values.bind(py),
                factories.bind(py),
            )
            .ok()?;
            typed.classes[at].omitted = omitted;
        }
    }
    Some(typed)
}

fn class_plan(
    py: Python<'_>,
    definition: &Definition<PyRules>,
    lowered: &Definition<NativeRules>,
) -> Option<PlanClass> {
    let direct = definition.class.direct.as_ref()?;
    let mut fields = Vec::with_capacity(definition.fields.len());
    let mut renamed = Vec::new();
    for (field, shadow) in definition.fields.iter().zip(&lowered.fields) {
        if field.name != field.wire_name {
            renamed.push(field.name.as_bytes().into());
        }
        fields.push(PlanField {
            key: field.wire_name.as_bytes().into(),
            pattern: if field
                .wire_name
                .bytes()
                .any(|byte| matches!(byte, b'"' | b'\\' | 0..=0x1f))
            {
                Box::default()
            } else {
                format!("\"{}\":", field.wire_name).into_bytes().into()
            },
            name: PyString::intern(py, &field.name).unbind(),
            wire: PyString::intern(py, &field.wire_name).unbind(),
            required: field.required,
            plan: plan_of(&shadow.node)?,
        });
    }
    let index = (fields.len() > SCAN_LIMIT).then(|| {
        fields
            .iter()
            .enumerate()
            .map(|(at, field)| (field.key.clone(), at))
            .collect()
    });
    Some(PlanClass {
        class: definition.class.object.clone_ref(py),
        layout: layout_of(py, definition.class.object.bind(py), &fields, direct),
        fields,
        forbid_extra: lowered.forbid_extra,
        direct_fields: direct.fields,
        supplied: direct.supplied.as_ref().map(|s| s.clone_ref(py)),
        renamed,
        index,
        omitted: Vec::new(),
    })
}

fn plan_of(node: &Node<NativeRules>) -> Option<Plan> {
    let plain = node.collection.is_none()
        && !node.normalize.is_set()
        && node.pattern.is_none()
        && node.one_of.is_none()
        && node.member.is_none()
        && node.multiple_of.is_none();
    if !plain {
        return None;
    }
    let sized = || (node.min_length, node.max_length.or(node.input_max_length));
    let shape = match &node.kind {
        Kind::Str => {
            let (min, max) = sized();
            Shape::Str { min, max }
        }
        Kind::Int => Shape::Int(bounds_of(node)?),
        Kind::Float => Shape::Float(bounds_of(node)?),
        Kind::Bool => Shape::Bool,
        Kind::Datetime if is_unconstrained(node) => Shape::Datetime,
        Kind::Schema(index) => Shape::Schema(*index),
        Kind::List(item) => {
            let (min, max) = sized();
            Shape::List {
                min,
                max,
                item: Box::new(plan_of(item)?),
            }
        }
        _ => return None,
    };
    Some(Plan {
        nullable: node.nullable,
        shape,
    })
}

fn is_unconstrained(node: &Node<NativeRules>) -> bool {
    node.min_length.is_none()
        && node.max_length.is_none()
        && node.input_max_length.is_none()
        && node.ge.is_none()
        && node.le.is_none()
        && node.gt.is_none()
        && node.lt.is_none()
        && node.max_digits.is_none()
        && node.decimal_places.is_none()
}

fn bounds_of(node: &Node<NativeRules>) -> Option<Bounds> {
    let number = |limit: &Option<NativeLimit>| match limit {
        None => Some(None),
        Some(limit) => limit.number().map(Some),
    };
    Some(Bounds {
        ge: number(&node.ge)?,
        le: number(&node.le)?,
        gt: number(&node.gt)?,
        lt: number(&node.lt)?,
    })
}

/// The instance for `raw`, or `None` when the ordinary path must run.
pub(super) fn decode<'py>(
    py: Python<'py>,
    typed: &Typed,
    raw: &[u8],
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let mut reader = Reader {
        py,
        typed,
        input: raw,
        at: 0,
        pending: Vec::new(),
    };
    match reader.document() {
        Ok(instance) => Ok(Some(instance)),
        Err(Stop::Fallback) => Ok(None),
        Err(Stop::Raise(err)) => Err(*err),
    }
}

/// The result of reading a number token.
enum Num {
    Int(i64),
    Real(f64),
}

struct Reader<'a, 'py, 't> {
    py: Python<'py>,
    typed: &'t Typed,
    input: &'a [u8],
    at: usize,
    /// The values of the objects being read, one run of slots per open object.
    pending: Vec<Option<Bound<'py, PyAny>>>,
}

impl<'a, 'py, 't> Reader<'a, 'py, 't> {
    fn document(&mut self) -> Step<Bound<'py, PyAny>> {
        self.skip_space();
        let instance = self.object(self.typed.root, 0)?;
        self.skip_space();
        if self.at != self.input.len() {
            return Err(Stop::Fallback);
        }
        Ok(instance)
    }

    fn peek(&self) -> Option<u8> {
        self.input.get(self.at).copied()
    }

    fn skip_space(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn take(&mut self, byte: u8) -> Step<()> {
        if self.peek() == Some(byte) {
            self.at += 1;
            return Ok(());
        }
        Err(Stop::Fallback)
    }

    fn word(&mut self, word: &[u8]) -> Step<()> {
        match self.input.get(self.at..self.at + word.len()) {
            Some(head) if same(head, word) => {
                self.at += word.len();
                Ok(())
            }
            _ => Err(Stop::Fallback),
        }
    }

    fn value(&mut self, plan: &Plan, depth: usize) -> Step<Bound<'py, PyAny>> {
        self.skip_space();
        if self.peek() == Some(b'n') {
            self.word(b"null")?;
            return if plan.nullable {
                Ok(self.py.None().into_bound(self.py))
            } else {
                Err(Stop::Fallback)
            };
        }
        match &plan.shape {
            Shape::Str { min, max } => self.text(*min, *max),
            Shape::Int(bounds) => self.integer(bounds),
            Shape::Float(bounds) => self.float(bounds),
            Shape::Bool => self.boolean(),
            Shape::Datetime => Err(Stop::Fallback),
            Shape::List { min, max, item } => self.list(*min, *max, item, depth),
            Shape::Schema(index) => self.object(*index, depth),
        }
    }

    fn boolean(&mut self) -> Step<Bound<'py, PyAny>> {
        let flag = match self.peek() {
            Some(b't') => {
                self.word(b"true")?;
                true
            }
            Some(b'f') => {
                self.word(b"false")?;
                false
            }
            _ => return Err(Stop::Fallback),
        };
        Ok(PyBool::new(self.py, flag).to_owned().into_any())
    }

    fn text(&mut self, min: Option<i64>, max: Option<i64>) -> Step<Bound<'py, PyAny>> {
        let text = self.string()?;
        if min.is_some() || max.is_some() {
            let size = if text.is_ascii() {
                text.len()
            } else {
                text.chars().count()
            };
            within(size, min, max)?;
        }
        Ok(PyString::new(self.py, &text).into_any())
    }

    fn integer(&mut self, bounds: &Bounds) -> Step<Bound<'py, PyAny>> {
        let Num::Int(int) = self.number()? else {
            return Err(Stop::Fallback);
        };
        if !bounds.holds(Number::Int(int)) {
            return Err(Stop::Fallback);
        }
        Ok(PyInt::new(self.py, int).into_any())
    }

    fn float(&mut self, bounds: &Bounds) -> Step<Bound<'py, PyAny>> {
        let float = match self.number()? {
            // Round to nearest, ties to even: Python's int-to-float conversion.
            #[expect(
                clippy::cast_precision_loss,
                reason = "Python rounds int to float the same way"
            )]
            Num::Int(int) => int as f64,
            Num::Real(float) => float,
        };
        if !bounds.holds(Number::Float(float)) {
            return Err(Stop::Fallback);
        }
        Ok(PyFloat::new(self.py, float).into_any())
    }

    fn list(
        &mut self,
        min: Option<i64>,
        max: Option<i64>,
        item: &Plan,
        depth: usize,
    ) -> Step<Bound<'py, PyAny>> {
        self.enter(b'[', depth)?;
        let mut items = Vec::new();
        self.skip_space();
        if self.peek() == Some(b']') {
            self.at += 1;
        } else {
            loop {
                items.push(self.value(item, depth + 1)?);
                if self.separator(b']')? {
                    break;
                }
            }
        }
        within(items.len(), min, max)?;
        Ok(PyList::new(self.py, items)?.into_any())
    }

    /// After a member or item: `true` on the closing byte, `false` on a comma.
    fn separator(&mut self, close: u8) -> Step<bool> {
        self.skip_space();
        match self.peek() {
            Some(b',') => {
                self.at += 1;
                Ok(false)
            }
            Some(byte) if byte == close => {
                self.at += 1;
                Ok(true)
            }
            _ => Err(Stop::Fallback),
        }
    }

    fn enter(&mut self, open: u8, depth: usize) -> Step<()> {
        if depth >= MAX_NESTING {
            return Err(Stop::Fallback);
        }
        self.take(open)
    }

    /// The raw bytes of a key without escapes. A key that names a field is
    /// valid UTF-8 because the field's name is; any other key is validated by
    /// the caller.
    fn key(&mut self) -> Step<&'a [u8]> {
        self.take(b'"')?;
        let start = self.at;
        while let Some(byte) = self.peek() {
            match byte {
                b'"' => {
                    let key = &self.input[start..self.at];
                    self.at += 1;
                    return Ok(key);
                }
                b'\\' | 0..=0x1f => return Err(Stop::Fallback),
                _ => self.at += 1,
            }
        }
        Err(Stop::Fallback)
    }

    fn object(&mut self, index: usize, depth: usize) -> Step<Bound<'py, PyAny>> {
        self.enter(b'{', depth)?;
        let typed: &'t Typed = self.typed;
        let class = &typed.classes[index];
        if let Some(layout) = &class.layout {
            return self.stored(index, class, layout, depth);
        }
        let base = self.pending.len();
        self.pending.resize_with(base + class.fields.len(), || None);
        let read = self.members(class, base, depth);
        let built = read.and_then(|()| self.close(index, class, base));
        self.pending.truncate(base);
        built
    }

    /// An object of a class with a slot layout: each value goes into its
    /// slot as it is read, and a bit mask tells which fields were seen.
    fn stored(
        &mut self,
        index: usize,
        class: &PlanClass,
        layout: &Layout,
        depth: usize,
    ) -> Step<Bound<'py, PyAny>> {
        let py = self.py;
        let instance = allocate(class.class.bind(py))?;
        let seen = self.fill_slots(class, layout, &instance, depth)?;
        if seen & layout.required != layout.required {
            return Err(Stop::Fallback);
        }
        if seen == layout.full {
            if let (Some(offset), Some(supplied)) = (layout.supplied, &class.supplied) {
                store(&instance, offset, supplied.bind(py).clone());
            }
            return Ok(instance);
        }
        let record = PyDict::new(py);
        for (at, field) in class.fields.iter().enumerate() {
            if seen & (1 << at) != 0 {
                record.set_item(field.name.bind(py), instance.getattr(field.name.bind(py))?)?;
            }
        }
        self.construct(index, class, record, Some(instance))
    }

    /// The members of an object, each value stored in its slot; the mask of the
    /// fields seen.
    fn fill_slots(
        &mut self,
        class: &PlanClass,
        layout: &Layout,
        instance: &Bound<'py, PyAny>,
        depth: usize,
    ) -> Step<u128> {
        let mut seen = 0u128;
        self.skip_space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(seen);
        }
        let mut hint = 0;
        loop {
            match self.member(class, hint)? {
                Ok(at) => {
                    if seen & (1 << at) != 0 {
                        return Err(Stop::Fallback);
                    }
                    let value = self.value(&class.fields[at].plan, depth + 1)?;
                    store(instance, layout.offsets[at], value);
                    seen |= 1 << at;
                    hint = at + 1;
                }
                Err(key) => self.extra(class, key, depth)?,
            }
            if self.separator(b'}')? {
                return Ok(seen);
            }
        }
    }

    /// The field the next member names, with its colon consumed; or, for a
    /// key that names none, that key. The field expected next is matched
    /// against the bytes directly, without scanning the key first.
    fn member(&mut self, class: &PlanClass, hint: usize) -> Step<Result<usize, &'a [u8]>> {
        self.skip_space();
        if let Some(field) = class.fields.get(hint) {
            let pattern = &*field.pattern;
            if let Some(head) = self.input.get(self.at..self.at + pattern.len()) {
                if !pattern.is_empty() && same(head, pattern) {
                    self.at += pattern.len();
                    return Ok(Ok(hint));
                }
            }
            let key = &*field.key;
            let end = self.at + 1 + key.len();
            if self.peek() == Some(b'"')
                && self.input.get(self.at + 1..end) == Some(key)
                && self.input.get(end) == Some(&b'"')
            {
                self.at = end + 1;
                self.skip_space();
                self.take(b':')?;
                return Ok(Ok(hint));
            }
        }
        let key = self.key()?;
        self.skip_space();
        self.take(b':')?;
        Ok(find(class, key, hint).ok_or(key))
    }

    /// A key that names no field: skipped, unless it must be refused.
    fn extra(&mut self, class: &PlanClass, key: &[u8], depth: usize) -> Step<()> {
        if class.forbid_extra
            || class.renamed.iter().any(|name| **name == *key)
            || std::str::from_utf8(key).is_err()
        {
            return Err(Stop::Fallback);
        }
        self.skip(depth + 1)
    }

    /// The instance from `record`: defaults the plan vouches for are completed
    /// in Rust (as `validate` and the constructor do), otherwise the class's
    /// own constructor fills them; a refusal there is reported, with its path,
    /// by the ordinary path.
    fn construct(
        &self,
        index: usize,
        class: &PlanClass,
        record: Bound<'py, PyDict>,
        held: Option<Bound<'py, PyAny>>,
    ) -> Step<Bound<'py, PyAny>> {
        let py = self.py;
        if let Some(done) = complete(self.typed, index, &record)? {
            let instance = match held {
                Some(instance) => instance,
                None => self
                    .typed
                    .allocate
                    .bind(py)
                    .call1((class.class.bind(py),))?,
            };
            for field in &class.fields {
                let name = field.name.bind(py);
                if let Some(value) = record.get_item(name)? {
                    assign(&instance, name, &value)?;
                }
            }
            if let Some(supplied) = done.supplied {
                assign(&instance, self.typed.supplied.bind(py), &supplied)?;
            }
            return Ok(instance);
        }
        match class
            .class
            .bind(py)
            .call_method1("__hinoki_construct_validated__", (record, ""))
        {
            Ok(built) => Ok(built),
            Err(err) if refusal(py, &err) => Err(Stop::Fallback),
            Err(err) => Err(Stop::from(err)),
        }
    }

    fn members(&mut self, class: &PlanClass, base: usize, depth: usize) -> Step<()> {
        self.skip_space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(());
        }
        let mut hint = 0;
        loop {
            self.skip_space();
            let key = self.key()?;
            self.skip_space();
            self.take(b':')?;
            match find(class, key, hint) {
                Some(at) => {
                    if self.pending[base + at].is_some() {
                        return Err(Stop::Fallback);
                    }
                    let value = self.value(&class.fields[at].plan, depth + 1)?;
                    self.pending[base + at] = Some(value);
                    hint = at + 1;
                }
                None => self.extra(class, key, depth)?,
            }
            if self.separator(b'}')? {
                return Ok(());
            }
        }
    }

    /// The instance from the values read into `pending[base..]`.
    fn close(&mut self, index: usize, class: &PlanClass, base: usize) -> Step<Bound<'py, PyAny>> {
        let py = self.py;
        let slots = &mut self.pending[base..];
        let present = slots.iter().filter(|slot| slot.is_some()).count();
        let missing = class
            .fields
            .iter()
            .zip(slots.iter())
            .any(|(field, slot)| field.required && slot.is_none());
        if missing {
            return Err(Stop::Fallback);
        }
        let class_object = class.class.bind(py);
        if present == class.direct_fields {
            let instance = self.typed.allocate.bind(py).call1((class_object,))?;
            for (field, slot) in class.fields.iter().zip(slots.iter_mut()) {
                if let Some(value) = slot.take() {
                    assign(&instance, field.name.bind(py), &value)?;
                }
            }
            if let Some(supplied) = &class.supplied {
                assign(&instance, self.typed.supplied.bind(py), supplied.bind(py))?;
            }
            return Ok(instance);
        }
        let record = PyDict::new(py);
        fill(&record, class, slots)?;
        self.construct(index, class, record, None)
    }

    /// A string value: its text, escapes resolved. A lone surrogate or a NUL
    /// is left to the ordinary path.
    #[allow(unsafe_code)] // see the SAFETY comments in the body
    fn string(&mut self) -> Step<Cow<'a, str>> {
        self.take(b'"')?;
        let mut owned: Option<String> = None;
        loop {
            let start = self.at;
            self.at = start + plain_run(&self.input[start..]);
            let bytes = &self.input[start..self.at];
            let run = if bytes.is_ascii() {
                // SAFETY: every byte of `bytes` is below 0x80, which is ASCII
                // and so valid UTF-8.
                unsafe { std::str::from_utf8_unchecked(bytes) }
            } else {
                std::str::from_utf8(bytes).map_err(|_| Stop::Fallback)?
            };
            match self.peek() {
                Some(b'"') => {
                    self.at += 1;
                    return Ok(match owned {
                        None => Cow::Borrowed(run),
                        Some(mut text) => {
                            text.push_str(run);
                            Cow::Owned(text)
                        }
                    });
                }
                Some(b'\\') => {
                    let text = owned.get_or_insert_with(String::new);
                    text.push_str(run);
                    self.at += 1;
                    self.escape(text)?;
                }
                _ => return Err(Stop::Fallback),
            }
        }
    }

    fn escape(&mut self, out: &mut String) -> Step<()> {
        let letter = self.peek().ok_or(Stop::Fallback)?;
        self.at += 1;
        out.push(match letter {
            b'"' => '"',
            b'\\' => '\\',
            b'/' => '/',
            b'b' => '\u{8}',
            b'f' => '\u{c}',
            b'n' => '\n',
            b'r' => '\r',
            b't' => '\t',
            b'u' => self.unicode()?,
            _ => return Err(Stop::Fallback),
        });
        Ok(())
    }

    fn unicode(&mut self) -> Step<char> {
        let first = self.hex4()?;
        let code = if (0xD800..0xDC00).contains(&first) {
            self.word(b"\\u")?;
            let second = self.hex4()?;
            if !(0xDC00..0xE000).contains(&second) {
                return Err(Stop::Fallback);
            }
            0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00)
        } else {
            first
        };
        match char::from_u32(code) {
            Some('\0') | None => Err(Stop::Fallback),
            Some(found) => Ok(found),
        }
    }

    fn hex4(&mut self) -> Step<u32> {
        let digits = self.input.get(self.at..self.at + 4).ok_or(Stop::Fallback)?;
        let mut code = 0;
        for &digit in digits {
            code = code * 16 + char::from(digit).to_digit(16).ok_or(Stop::Fallback)?;
        }
        self.at += 4;
        Ok(code)
    }

    /// The digits of an integer part, as their value (`None` past `u64`).
    fn digits(&mut self) -> Step<Option<u64>> {
        let start = self.at;
        let mut value = Some(0u64);
        while let Some(byte @ b'0'..=b'9') = self.peek() {
            value = value.and_then(|acc| acc.checked_mul(10)?.checked_add(u64::from(byte - b'0')));
            self.at += 1;
        }
        if self.at == start {
            return Err(Stop::Fallback);
        }
        Ok(value)
    }

    /// A number token by the JSON grammar; an integer outside `i64` and a
    /// float outside `f64` are left to the ordinary path.
    #[allow(unsafe_code)] // see the SAFETY comments in the body
    fn number(&mut self) -> Step<Num> {
        let start = self.at;
        let negative = self.peek() == Some(b'-');
        if negative {
            self.at += 1;
        }
        let whole = if self.peek() == Some(b'0') {
            self.at += 1;
            Some(0)
        } else {
            self.digits()?
        };
        let fraction = self.fraction()?;
        let exponent = self.exponent()?;
        if fraction.is_none() && !exponent {
            return integer(whole, negative);
        }
        if let (false, Some(whole), Some((Some(frac), places))) = (exponent, whole, fraction) {
            if let Some(float) = exact_decimal(whole, frac, places) {
                return Ok(Num::Real(if negative { -float } else { float }));
            }
        }
        // SAFETY: the token is a sign, ASCII digits, '.', 'e' or 'E', which
        // the scan above accepted: all ASCII, so valid UTF-8.
        let text = unsafe { std::str::from_utf8_unchecked(&self.input[start..self.at]) };
        let float = text.parse::<f64>().map_err(|_| Stop::Fallback)?;
        if float.is_finite() {
            Ok(Num::Real(float))
        } else {
            Err(Stop::Fallback)
        }
    }

    /// The `.digits` part of a number, if there is one: the digits and how
    /// many places they take.
    fn fraction(&mut self) -> Step<Option<(Option<u64>, usize)>> {
        if self.peek() != Some(b'.') {
            return Ok(None);
        }
        self.at += 1;
        let from = self.at;
        let frac = self.digits()?;
        Ok(Some((frac, self.at - from)))
    }

    /// Consume an `e`/`E` exponent, if there is one.
    fn exponent(&mut self) -> Step<bool> {
        if !matches!(self.peek(), Some(b'e' | b'E')) {
            return Ok(false);
        }
        self.at += 1;
        if matches!(self.peek(), Some(b'+' | b'-')) {
            self.at += 1;
        }
        self.digits()?;
        Ok(true)
    }

    /// A value that no field reads: it must still be a valid JSON value.
    fn skip(&mut self, depth: usize) -> Step<()> {
        self.skip_space();
        match self.peek() {
            Some(b'{') => self.skip_object(depth),
            Some(b'[') => self.skip_array(depth),
            Some(b'"') => self.string().map(drop),
            Some(b't') => self.word(b"true"),
            Some(b'f') => self.word(b"false"),
            Some(b'n') => self.word(b"null"),
            Some(b'-' | b'0'..=b'9') => self.number().map(drop),
            _ => Err(Stop::Fallback),
        }
    }

    fn skip_object(&mut self, depth: usize) -> Step<()> {
        self.enter(b'{', depth)?;
        self.skip_space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(());
        }
        loop {
            self.skip_space();
            self.string()?;
            self.skip_space();
            self.take(b':')?;
            self.skip(depth + 1)?;
            if self.separator(b'}')? {
                return Ok(());
            }
        }
    }

    fn skip_array(&mut self, depth: usize) -> Step<()> {
        self.enter(b'[', depth)?;
        self.skip_space();
        if self.peek() == Some(b']') {
            self.at += 1;
            return Ok(());
        }
        loop {
            self.skip(depth + 1)?;
            if self.separator(b']')? {
                return Ok(());
            }
        }
    }
}

/// Fallback unless `size` is inside the declared length bounds.
fn within(size: usize, min: Option<i64>, max: Option<i64>) -> Step<()> {
    let size = i64::try_from(size).unwrap_or(i64::MAX);
    if min.is_some_and(|least| size < least) || max.is_some_and(|most| size > most) {
        return Err(Stop::Fallback);
    }
    Ok(())
}

/// `object.__setattr__(instance, name, value)`: what a frozen dataclass's own
/// generated constructor does for each field, without the Python call.
#[allow(unsafe_code)] // see the SAFETY comments in the body
pub(super) fn assign(
    instance: &Bound<'_, PyAny>,
    name: &Bound<'_, PyString>,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    // SAFETY: the three pointers belong to live objects held by the GIL
    // thread for the whole call, `name` is a `str`, and
    // `PyObject_GenericSetAttr` accepts any object; on failure it returns -1
    // with the exception set, which `fetch` takes.
    let status = unsafe {
        pyo3::ffi::PyObject_GenericSetAttr(instance.as_ptr(), name.as_ptr(), value.as_ptr())
    };
    if status == 0 {
        return Ok(());
    }
    Err(PyErr::fetch(instance.py()))
}

/// The slot offsets of the fields of `class`, when every field is a plain
/// object slot of a heap type and the class can be allocated like
/// `object.__new__` does.
#[allow(unsafe_code)] // see the SAFETY comments in the body
fn layout_of(
    py: Python<'_>,
    class: &Bound<'_, PyAny>,
    fields: &[PlanField],
    direct: &super::Direct,
) -> Option<Layout> {
    let class_type = class.cast::<PyType>().ok()?;
    if fields.is_empty() || fields.len() > 128 || fields.len() != direct.fields {
        return None;
    }
    // SAFETY: `class_type` is a live type object.
    let flags = unsafe { pyo3::ffi::PyType_GetFlags(class_type.as_type_ptr()) };
    if flags & pyo3::ffi::Py_TPFLAGS_IS_ABSTRACT != 0 {
        return None;
    }
    let basicsize: isize = class.getattr("__basicsize__").ok()?.extract().ok()?;
    let mro = class.getattr("__mro__").ok()?.cast_into::<PyTuple>().ok()?;
    let offset = |name: &str| member_offset(&mro, name, basicsize);
    let offsets = fields
        .iter()
        .map(|field| offset(field.name.bind(py).to_str().ok()?))
        .collect::<Option<Vec<_>>>()?;
    let supplied = match direct.supplied {
        Some(_) => Some(offset("__hinoki_supplied_fields__")?),
        None => None,
    };
    Some(Layout {
        offsets,
        supplied,
        required: fields
            .iter()
            .enumerate()
            .filter(|(_, field)| field.required)
            .fold(0, |mask, (at, _)| mask | 1 << at),
        full: if fields.len() == 128 {
            u128::MAX
        } else {
            (1 << fields.len()) - 1
        },
    })
}

/// The offset of the slot `name` of the first class of `mro` that defines it,
/// or `None` when that definition is anything but a slot.
fn member_offset(mro: &Bound<'_, PyTuple>, name: &str, basicsize: isize) -> Option<isize> {
    for klass in mro.iter() {
        let Ok(entry) = klass.getattr("__dict__").ok()?.get_item(name) else {
            continue;
        };
        if entry.get_type().name().ok()? != "member_descriptor" {
            return None;
        }
        return slot_offset(klass.cast::<PyType>().ok()?, name, basicsize);
    }
    None
}

#[allow(unsafe_code)] // see the SAFETY comments in the body
fn slot_offset(klass: &Bound<'_, PyType>, name: &str, basicsize: isize) -> Option<isize> {
    use pyo3::ffi;
    let ptr = klass.as_type_ptr();
    // SAFETY: `PyType_GetSlot` may be asked for `tp_members` of a heap type
    // only, which the flag test ensures. The array it returns ends with an
    // entry whose name is null, and every entry before it has a valid
    // NUL-terminated name; the walk stops there. `klass` is alive for the call.
    unsafe {
        if ffi::PyType_GetFlags(ptr) & ffi::Py_TPFLAGS_HEAPTYPE == 0 {
            return None;
        }
        let mut member = ffi::PyType_GetSlot(ptr, ffi::Py_tp_members).cast::<ffi::PyMemberDef>();
        if member.is_null() {
            return None;
        }
        while !(*member).name.is_null() {
            let def = *member;
            if std::ffi::CStr::from_ptr(def.name).to_bytes() == name.as_bytes() {
                let storable = def.type_code == ffi::Py_T_OBJECT_EX
                    && def.flags & ffi::Py_READONLY == 0
                    && def.offset >= 0
                    && def.offset % 8 == 0
                    && def.offset + 8 <= basicsize;
                return storable.then_some(def.offset);
            }
            member = member.add(1);
        }
    }
    None
}

/// A new instance of `class` with every slot empty, as `object.__new__` makes.
#[allow(unsafe_code)] // see the SAFETY comments in the body
fn allocate<'py>(class: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let class_type = class.cast::<PyType>()?;
    // SAFETY: `class_type` is a live type whose layout was checked when the
    // plan was made (a plain object layout, not abstract, `object.__new__` as
    // its allocator). `PyType_GenericAlloc` returns a zeroed new reference or
    // null with the exception set, which `from_owned_ptr_or_err` takes.
    unsafe {
        Bound::from_owned_ptr_or_err(
            class.py(),
            pyo3::ffi::PyType_GenericAlloc(class_type.as_type_ptr(), 0),
        )
    }
}

/// Put `value` in the slot at `offset` of `instance`.
#[allow(unsafe_code)] // see the SAFETY comments in the body
fn store(instance: &Bound<'_, PyAny>, offset: isize, value: Bound<'_, PyAny>) {
    // SAFETY: `offset` is the offset of an object slot of `instance`'s class,
    // checked against its basic size when the plan was made, and the slot is
    // either empty or holds an owned reference, which is released. The
    // instance is alive and its slots are only touched under the GIL.
    unsafe {
        let place = instance
            .as_ptr()
            .cast::<u8>()
            .offset(offset)
            .cast::<*mut pyo3::ffi::PyObject>();
        let old = std::mem::replace(&mut *place, value.into_ptr());
        pyo3::ffi::Py_XDECREF(old);
    }
}

/// The read values, in declaration order, as the entries of `target`.
fn fill<'py>(
    target: &Bound<'py, PyDict>,
    class: &PlanClass,
    slots: &mut [Option<Bound<'py, PyAny>>],
) -> PyResult<()> {
    let py = target.py();
    for (field, slot) in class.fields.iter().zip(slots.iter_mut()) {
        if let Some(value) = slot.take() {
            target.set_item(field.name.bind(py), value)?;
        }
    }
    Ok(())
}

/// An integer token as an `i64`; one outside the range is left to the ordinary path.
fn integer(whole: Option<u64>, negative: bool) -> Step<Num> {
    let whole = whole.ok_or(Stop::Fallback)?;
    let int = if negative {
        0i64.checked_sub_unsigned(whole)
    } else {
        i64::try_from(whole).ok()
    };
    int.map(Num::Int).ok_or(Stop::Fallback)
}

/// `whole.frac` as a float when the digits fit in 2^53 and the scale in 10^22:
/// both are then exact `f64`s, and one division rounds correctly (Clinger).
fn exact_decimal(whole: u64, frac: u64, places: usize) -> Option<f64> {
    const POWERS: [u64; 20] = {
        let mut table = [1u64; 20];
        let mut at = 1;
        while at < 20 {
            table[at] = table[at - 1] * 10;
            at += 1;
        }
        table
    };
    let scale = *POWERS.get(places)?;
    let mantissa = whole.checked_mul(scale)?.checked_add(frac)?;
    if mantissa > 1 << 53 {
        return None;
    }
    #[expect(
        clippy::cast_precision_loss,
        reason = "the mantissa is at most 2^53, exact in f64"
    )]
    Some(mantissa as f64 / scale as f64)
}

/// The length of the run of bytes at the start of `bytes` that need no
/// attention in a string: no quote, no backslash, no control character. Eight
/// bytes at a time; the lowest flagged byte of a word is exact.
fn plain_run(bytes: &[u8]) -> usize {
    const LOW: u64 = 0x0101_0101_0101_0101;
    const HIGH: u64 = 0x8080_8080_8080_8080;
    let mut at = 0;
    let (words, _) = bytes.as_chunks::<8>();
    for chunk in words {
        let word = u64::from_le_bytes(*chunk);
        let quote = word ^ (LOW * u64::from(b'"'));
        let slash = word ^ (LOW * u64::from(b'\\'));
        let flagged = (word.wrapping_sub(LOW * 0x20) & !word)
            | (quote.wrapping_sub(LOW) & !quote)
            | (slash.wrapping_sub(LOW) & !slash);
        let flagged = flagged & HIGH;
        if flagged != 0 {
            return at + (flagged.trailing_zeros() / 8) as usize;
        }
        at += 8;
    }
    at + bytes[at..]
        .iter()
        .take_while(|&&byte| byte != b'"' && byte != b'\\' && byte >= 0x20)
        .count()
}

/// Whether two slices of one length hold the same bytes, compared a word at a
/// time (the keys are short, where a call to `memcmp` costs more than the loop).
fn same(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let (lefts, left_rest) = left.as_chunks::<8>();
    let (rights, right_rest) = right.as_chunks::<8>();
    lefts
        .iter()
        .zip(rights)
        .all(|(a, b)| u64::from_ne_bytes(*a) == u64::from_ne_bytes(*b))
        && left_rest == right_rest
}

/// The position of the field read from `key`, trying the one after the last
/// match first: bodies mostly list their members in declaration order.
fn find(class: &PlanClass, key: &[u8], hint: usize) -> Option<usize> {
    if class
        .fields
        .get(hint)
        .is_some_and(|field| *field.key == *key)
    {
        return Some(hint);
    }
    match &class.index {
        Some(index) => index.get(key).copied(),
        None => class.fields.iter().position(|field| *field.key == *key),
    }
}

/// A `ValidationError` raised by a constructor: the ordinary path reports it.
fn refusal(py: Python<'_>, err: &PyErr) -> bool {
    err.get_type(py)
        .name()
        .is_ok_and(|name| name == "ValidationError")
}
