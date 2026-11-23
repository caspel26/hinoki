use std::borrow::Cow;
use std::collections::HashSet;
use std::marker::PhantomData;
use std::sync::Arc;

use pyo3::basic::CompareOp;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use super::coerce;
use super::internet::NetKind;
use super::model::{
    Collection, Compiled, Defer, Definition, Kind, NativeSchema, Node, Options, Problem, Rules,
    Validated, MAX_DECIMAL_DIGITS, MAX_DEPTH, MAX_ERRORS, MAX_KEY_ECHO,
};
use super::scalars::{self, DurationError};
use super::typeddecode;
use super::views::{JsonView, PyView, View, Wire};
use crate::request::Json;

enum Abort<E> {
    TooMany,
    Depth,
    View(E),
}

type Checked<V> = Result<Option<<V as View>::Out>, Abort<<V as View>::Error>>;
type Outcome<V> = Result<(Option<<V as View>::Out>, Vec<Problem>), <V as View>::Error>;

fn join(path: &str, name: &str) -> String {
    if path.is_empty() {
        name.to_owned()
    } else {
        format!("{path}.{name}")
    }
}

struct Run<'a, V: View> {
    view: V,
    definitions: &'a [Definition<V::Rules>],
    options: &'a Options,
    problems: Vec<Problem>,
    /// Plain unions being tried right now, and the failed container trials
    /// among them: the work of nested unions multiplies, so it is capped.
    trials: usize,
    wasted: usize,
}

/// Failed container variants one outermost union may burn before the whole
/// validation is refused.
const MAX_WASTED_TRIALS: usize = 1024;

pub(super) fn is_composite<R: Rules>(kind: &Kind<R>) -> bool {
    matches!(
        kind,
        Kind::AnyOf(_)
            | Kind::List(_)
            | Kind::Tuple(_)
            | Kind::Map { .. }
            | Kind::Schema(_)
            | Kind::TaggedUnion { .. }
    )
}

impl<'a, V: View> Run<'a, V> {
    fn new(view: V, definitions: &'a [Definition<V::Rules>], options: &'a Options) -> Self {
        Run {
            view,
            definitions,
            options,
            problems: Vec::new(),
            trials: 0,
            wasted: 0,
        }
    }

    /// `(value, problems)` of the root schema at `root`: exactly one of them
    /// is meaningful.
    fn validate(mut self, root: usize, data: &V::Data) -> Outcome<V> {
        match self.schema(root, data, "", 0) {
            Ok(value) => Ok((value, self.problems)),
            Err(Abort::TooMany) => {
                let mut problems = self.problems;
                problems.push((
                    String::new(),
                    "too_many_errors",
                    "further errors omitted".to_owned(),
                ));
                Ok((None, problems))
            }
            Err(Abort::Depth) => Ok((
                None,
                vec![(
                    String::new(),
                    "depth",
                    "input is nested too deeply".to_owned(),
                )],
            )),
            Err(Abort::View(err)) => Err(err),
        }
    }

    fn fail(&mut self, path: &str, code: &'static str, message: String) -> Checked<V> {
        self.problems.push((path.to_owned(), code, message));
        if self.problems.len() >= MAX_ERRORS {
            return Err(Abort::TooMany);
        }
        Ok(None)
    }

    fn check(
        &mut self,
        node: &Node<V::Rules>,
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        if depth > MAX_DEPTH {
            return Err(Abort::Depth);
        }
        if self.view.is_null(data) {
            if node.nullable {
                return Ok(Some(self.view.null(data)));
            }
            return self.fail(path, "null", "null is not allowed".to_owned());
        }
        let checked = match &node.kind {
            Kind::Str => self.text(node, data, path)?,
            Kind::Int => self.integer(node, data, path)?,
            Kind::Float => self.number(node, data, path)?,
            Kind::Bool => self.boolean(data, path)?,
            Kind::Uuid => self.uuid(data, path)?,
            Kind::Datetime => self.moment(data, path)?,
            Kind::Date => self.day(data, path)?,
            Kind::Time => self.clock(data, path)?,
            Kind::Timedelta => self.duration(data, path)?,
            Kind::Decimal => self.decimal(node, data, path)?,
            Kind::Bytes => self.binary(node, data, path)?,
            Kind::Net(kind) => self.internet(*kind, data, path)?,
            Kind::Path => self.file_path(node, data, path)?,
            Kind::Secret => self.secret(node, data, path)?,
            Kind::List(item) => self.sequence(node, item, data, path, depth)?,
            Kind::Tuple(positions) => self.fixed(node, positions, data, path, depth)?,
            Kind::AnyOf(variants) => self.any_of(variants, data, path, depth)?,
            Kind::Any => Some(self.view.anything(data).map_err(Abort::View)?),
            Kind::Map { key, value } => self.mapping(node, key, value, data, path, depth)?,
            Kind::Schema(index) => self.schema(*index, data, path, depth)?,
            Kind::TaggedUnion {
                discriminator,
                discriminator_wire_name,
                variants,
            } => self.tagged_union(
                discriminator,
                discriminator_wire_name,
                variants,
                data,
                path,
                depth,
            )?,
        };
        self.settle(node, checked, path)
    }

    /// The checked value after `one_of` and, for an `Enum` field, as its member.
    fn settle(&mut self, node: &Node<V::Rules>, checked: Option<V::Out>, path: &str) -> Checked<V> {
        let checked = match (&node.one_of, checked) {
            (Some(choices), Some(value)) => self.one_of(choices, value, path)?,
            (_, other) => other,
        };
        let checked = match (&node.member, checked) {
            (Some(class), Some(value)) => self.member(class, value)?,
            (_, other) => other,
        };
        match (node.collection, checked) {
            (Some(collection), Some(value)) => self.collect(collection, value),
            (_, other) => Ok(other),
        }
    }

    /// The checked array as a set, frozenset or tuple.
    fn collect(&mut self, collection: Collection, value: V::Out) -> Checked<V> {
        self.view
            .collect(collection, value)
            .map(Some)
            .map_err(Abort::View)
    }

    /// The member of the field's `Enum` that the checked value names.
    fn member(&mut self, class: &<V::Rules as Rules>::Member, value: V::Out) -> Checked<V> {
        self.view
            .member(class, value)
            .map(Some)
            .map_err(Abort::View)
    }

    /// The value must equal one of the declared choices (strict types were
    /// already enforced by the kind, so `True` never passes as an integer).
    fn one_of(
        &mut self,
        choices: &<V::Rules as Rules>::Choices,
        value: V::Out,
        path: &str,
    ) -> Checked<V> {
        if self.view.is_choice(&value, choices).map_err(Abort::View)? {
            return Ok(Some(value));
        }
        let shown = self.view.choices_shown(choices);
        self.fail(path, "one_of", format!("must be one of {shown}"))
    }

    /// The `max_length` of `node`, or its wire-input bound on wire input.
    fn most(&self, node: &Node<V::Rules>) -> Option<i64> {
        node.max_length.or(if self.options.input_bounds {
            node.input_max_length
        } else {
            None
        })
    }

    fn length(
        &mut self,
        node: &Node<V::Rules>,
        size: usize,
        path: &str,
        unit: &str,
    ) -> Result<bool, Abort<V::Error>> {
        let size = i64::try_from(size).unwrap_or(i64::MAX);
        if let Some(least) = node.min_length {
            if size < least {
                let unit = if least == 1 {
                    unit.to_owned()
                } else {
                    format!("{unit}s")
                };
                self.fail(path, "min_length", format!("at least {least} {unit}"))?;
                return Ok(false);
            }
        }
        if let Some(most) = self.most(node) {
            if size > most {
                let unit = if most == 1 {
                    unit.to_owned()
                } else {
                    format!("{unit}s")
                };
                self.fail(path, "max_length", format!("at most {most} {unit}"))?;
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn text(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let Some(text) = self.view.string(data) else {
            return self.fail(path, "type", "expected a string".to_owned());
        };
        let text = if node.normalize.is_set() {
            self.view
                .normalized(text, node.normalize)
                .map_err(Abort::View)?
        } else {
            text
        };
        let size = self.view.char_count(&text).map_err(Abort::View)?;
        if !self.length(node, size, path, "character")? {
            return Ok(None);
        }
        let Ok(matched) = self.view.text(&text) else {
            return self.fail(path, "type", "must be valid Unicode text".to_owned());
        };
        if matched.contains('\0') {
            return self.fail(path, "nul", "must not contain NUL characters".to_owned());
        }
        if !self.pattern(node, Some(matched), path)? {
            return Ok(None);
        }
        Ok(Some(self.view.string_out(text)))
    }

    fn bounds(&mut self, node: &Node<V::Rules>, value: V::Out, path: &str) -> Checked<V> {
        let tests = [
            ("ge", "greater than or equal to", CompareOp::Ge, &node.ge),
            ("le", "less than or equal to", CompareOp::Le, &node.le),
            ("gt", "greater than", CompareOp::Gt, &node.gt),
            ("lt", "less than", CompareOp::Lt, &node.lt),
        ];
        for (name, words, op, limit) in tests {
            if let Some(limit) = limit {
                let failed = self
                    .view
                    .limit_fails(&value, limit, op)
                    .map_err(Abort::View)?;
                if let Some(shown) = failed {
                    return self.fail(path, name, format!("must be {words} {shown}"));
                }
            }
        }
        if let Some(step) = &node.multiple_of {
            let failed = self
                .view
                .multiple_fails(&value, step)
                .map_err(Abort::View)?;
            if let Some(shown) = failed {
                return self.fail(
                    path,
                    "multiple_of",
                    format!("must be a multiple of {shown}"),
                );
            }
        }
        Ok(Some(value))
    }

    fn integer(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let Some(value) = self.view.integer(data).map_err(Abort::View)? else {
            return self.fail(path, "type", "expected an integer".to_owned());
        };
        self.bounds(node, value, path)
    }

    fn number(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let Some(number) = self.view.number(data).map_err(Abort::View)? else {
            return self.fail(path, "type", "expected a number".to_owned());
        };
        let Some(value) = number else {
            return self.fail(path, "finite", "number is out of range".to_owned());
        };
        if !value.is_finite() {
            return self.fail(path, "finite", "number must be finite".to_owned());
        }
        let converted = self.view.float(value);
        self.bounds(node, converted, path)
    }

    fn boolean(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let Some(value) = self.view.boolean(data) else {
            return self.fail(path, "type", "expected a boolean".to_owned());
        };
        Ok(Some(value))
    }

    /// The content of a string input, `None` for anything else or a string
    /// that is not valid Unicode.
    fn plain_text(&self, data: &V::Data) -> Option<String> {
        let text = self.view.string(data)?;
        self.view.text(&text).ok().map(Cow::into_owned)
    }

    fn uuid(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data).filter(|text| is_canonical_uuid(text));
        let Some(text) = text else {
            return self.fail(path, "uuid", "expected a canonical UUID string".to_owned());
        };
        Ok(Some(self.view.uuid(data, &text).map_err(Abort::View)?))
    }

    fn moment(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let message = "expected an RFC 3339 datetime string";
        let text = self.plain_text(data).filter(|text| is_rfc3339(text));
        let Some(text) = text else {
            return self.fail(path, "datetime", message.to_owned());
        };
        let Some((parsed, naive)) = self.view.datetime(data, &text).map_err(Abort::View)? else {
            return self.fail(path, "datetime", message.to_owned());
        };
        if naive && !self.options.allow_naive_datetime {
            return self.fail(
                path,
                "datetime_tz",
                "datetime needs a UTC offset".to_owned(),
            );
        }
        Ok(Some(parsed))
    }

    fn day(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data);
        let parsed = text
            .as_deref()
            .and_then(|text| scalars::parse_date(text).map(|day| (day, text)));
        let Some((day, text)) = parsed else {
            let message = "expected an ISO 8601 date string";
            return self.fail(path, "date", message.to_owned());
        };
        Ok(Some(
            self.view.wire(Wire::Date(day), text).map_err(Abort::View)?,
        ))
    }

    fn clock(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data);
        let parsed = text
            .as_deref()
            .and_then(|text| scalars::parse_time(text).map(|clock| (clock, text)));
        let Some((clock, text)) = parsed else {
            let message = "expected an ISO 8601 time string";
            return self.fail(path, "time", message.to_owned());
        };
        if clock.offset.is_none() && !self.options.allow_naive_datetime {
            return self.fail(path, "time_tz", "time needs a UTC offset".to_owned());
        }
        Ok(Some(
            self.view
                .wire(Wire::Time(clock), text)
                .map_err(Abort::View)?,
        ))
    }

    fn file_path(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let text = self
            .plain_text(data)
            .filter(|text| !text.is_empty() && !text.contains('\0'));
        let Some(text) = text else {
            return self.fail(path, "path", "expected a file system path".to_owned());
        };
        if !self.length(node, text.chars().count(), path, "character")? {
            return Ok(None);
        }
        Ok(Some(
            self.view.wire(Wire::Path, &text).map_err(Abort::View)?,
        ))
    }

    fn secret(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        if self.text(node, data, path)?.is_none() {
            return Ok(None);
        }
        let Some(text) = self.plain_text(data) else {
            return self.fail(path, "type", "expected a string".to_owned());
        };
        Ok(Some(
            self.view.wire(Wire::Secret, &text).map_err(Abort::View)?,
        ))
    }

    fn internet(&mut self, kind: NetKind, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data).filter(|text| kind.accepts(text));
        let Some(text) = text else {
            return self.fail(path, kind.code(), kind.message().to_owned());
        };
        Ok(Some(
            self.view
                .wire(Wire::Net(kind), &text)
                .map_err(Abort::View)?,
        ))
    }

    fn duration(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data);
        let parsed = text
            .as_deref()
            .map(|text| scalars::parse_duration(text).map(|span| (span, text)));
        match parsed {
            Some(Ok((span, text))) => Ok(Some(
                self.view
                    .wire(Wire::Duration(span), text)
                    .map_err(Abort::View)?,
            )),
            Some(Err(DurationError::Range)) => self.fail(
                path,
                "duration_range",
                "duration is out of range".to_owned(),
            ),
            _ => {
                let message = "expected an ISO 8601 duration string";
                self.fail(path, "duration", message.to_owned())
            }
        }
    }

    fn decimal(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.view.decimal_text(data).map_err(Abort::View)?;
        let parsed = text
            .as_deref()
            .and_then(|text| scalars::decimal_shape(text).map(|shape| (shape, text)));
        let Some((shape, text)) = parsed else {
            let message = "expected a decimal string or an integer";
            return self.fail(path, "decimal", message.to_owned());
        };
        if !self.decimal_digits(node, shape, path)? {
            return Ok(None);
        }
        let value = self.view.wire(Wire::Decimal, text).map_err(Abort::View)?;
        self.bounds(node, value, path)
    }

    /// Whether a decimal has no more digits, decimal places and whole digits
    /// than the node allows; reports the first limit it breaks at `path`.
    fn decimal_digits(
        &mut self,
        node: &Node<V::Rules>,
        shape: scalars::DecimalShape,
        path: &str,
    ) -> Result<bool, Abort<V::Error>> {
        let most = node.max_digits.unwrap_or(MAX_DECIMAL_DIGITS);
        let whole = node.decimal_places.map(|places| most - places);
        let tests = [
            (shape.digits, Some(most), "max_digits", "digit", ""),
            (
                shape.places,
                node.decimal_places,
                "decimal_places",
                "decimal place",
                "",
            ),
            (
                shape.digits.saturating_sub(shape.places),
                whole,
                "max_whole_digits",
                "digit",
                " before the decimal point",
            ),
        ];
        for (count, limit, code, unit, after) in tests {
            let count = i64::try_from(count).unwrap_or(i64::MAX);
            let Some(limit) = limit.filter(|limit| count > *limit) else {
                continue;
            };
            let plural = if limit == 1 { "" } else { "s" };
            let message = format!("at most {limit} {unit}{plural}{after}");
            self.fail(path, code, message)?;
            return Ok(false);
        }
        Ok(true)
    }

    fn binary(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data);
        let decoded = text
            .as_deref()
            .and_then(|text| scalars::decode_base64(text).map(|raw| (raw, text)));
        let Some((raw, text)) = decoded else {
            return self.fail(path, "bytes", "expected a base64 string".to_owned());
        };
        if !self.length(node, raw.len(), path, "byte")? {
            return Ok(None);
        }
        if node.pattern.is_some() {
            // One character per byte (Latin-1), so Python's `re` sees the same text.
            let latin_1: String = raw.iter().map(|&byte| char::from(byte)).collect();
            if !self.pattern(node, Some(Cow::Owned(latin_1)), path)? {
                return Ok(None);
            }
        }
        Ok(Some(
            self.view
                .wire(Wire::Bytes(raw), text)
                .map_err(Abort::View)?,
        ))
    }

    fn pattern(
        &mut self,
        node: &Node<V::Rules>,
        subject: Option<Cow<'_, str>>,
        path: &str,
    ) -> Result<bool, Abort<V::Error>> {
        let Some((source, regex)) = &node.pattern else {
            return Ok(true);
        };
        if subject.is_some_and(|text| regex.is_match(&text)) {
            return Ok(true);
        }
        let shown = self.view.pattern_shown(source).map_err(Abort::View)?;
        self.fail(path, "pattern", format!("must match {shown}"))?;
        Ok(false)
    }

    fn sequence(
        &mut self,
        node: &Node<V::Rules>,
        item: &Node<V::Rules>,
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let Some(list) = self.view.list(data) else {
            return self.fail(path, "type", "expected an array".to_owned());
        };
        if !self.length(node, self.view.list_len(&list), path, "item")? {
            return Ok(None);
        }
        let before = self.problems.len();
        let mut out = self.view.new_list();
        for (index, element) in V::items(&list).enumerate() {
            let found = self.check(item, &element, &format!("{path}[{index}]"), depth + 1)?;
            if let Some(value) = found {
                self.view.push(&mut out, value).map_err(Abort::View)?;
            }
        }
        Ok(if self.problems.len() > before {
            None
        } else {
            Some(self.view.close_list(out))
        })
    }

    /// A `tuple[X, Y]` field: an array of exactly as many items as positions,
    /// each checked at `path[i]` by its position's node, the result a tuple.
    /// A plain union: each variant is tried on the same data in declared order;
    /// the trial's problems are dropped, and a value no variant takes is one
    /// `union` problem.
    fn any_of(
        &mut self,
        variants: &[Node<V::Rules>],
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        if self.trials == 0 {
            self.wasted = 0;
        }
        self.trials += 1;
        let found = self.try_variants(variants, data, path, depth);
        self.trials -= 1;
        found
    }

    fn try_variants(
        &mut self,
        variants: &[Node<V::Rules>],
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let before = self.problems.len();
        for variant in variants {
            if self.wasted > MAX_WASTED_TRIALS {
                return Err(Abort::TooMany);
            }
            let tried = match self.check(variant, data, path, depth + 1) {
                Err(Abort::TooMany) if self.wasted <= MAX_WASTED_TRIALS => None,
                other => other?,
            };
            self.problems.truncate(before);
            if tried.is_some() {
                return Ok(tried);
            }
            if self.trials > 1 && is_composite(&variant.kind) {
                self.wasted += 1;
            }
        }
        self.fail(path, "union", "does not match any allowed type".to_owned())
    }

    fn fixed(
        &mut self,
        node: &Node<V::Rules>,
        positions: &[Node<V::Rules>],
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let Some(list) = self.view.list(data) else {
            return self.fail(path, "type", "expected an array".to_owned());
        };
        if !self.length(node, self.view.list_len(&list), path, "item")? {
            return Ok(None);
        }
        let before = self.problems.len();
        let mut out = self.view.new_list();
        for ((index, element), position) in V::items(&list).enumerate().zip(positions) {
            let found = self.check(position, &element, &format!("{path}[{index}]"), depth + 1)?;
            if let Some(value) = found {
                self.view.push(&mut out, value).map_err(Abort::View)?;
            }
        }
        if self.problems.len() > before {
            return Ok(None);
        }
        let closed = self.view.close_list(out);
        self.collect(Collection::Tuple, closed)
    }

    /// A `dict[str, X]` field: an object of string keys, its entry count
    /// within the node's limits, each key within the key node's, each value
    /// checked at `path.key` (the key shortened as unknown fields are).
    fn mapping(
        &mut self,
        node: &Node<V::Rules>,
        key: &Node<V::Rules>,
        value: &Node<V::Rules>,
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let Some(object) = self.view.object(data) else {
            return self.fail(path, "type", "expected an object".to_owned());
        };
        let before = self.problems.len();
        self.validate_object_key_types(&object, path)?;
        if self.problems.len() > before {
            return Ok(None);
        }
        let entries = self.view.entries(&object).map_err(Abort::View)?;
        if !self.length(node, entries.len(), path, "item")? {
            return Ok(None);
        }
        let mut out = self.view.new_record();
        for (name, raw) in entries {
            let Some(name) = name else {
                let message = "object keys must be valid Unicode text".to_owned();
                self.fail(path, "type", message)?;
                continue;
            };
            let shown: String = name.chars().take(MAX_KEY_ECHO).collect();
            let here = join(path, &shown);
            if !self.map_key(key, &name, &here)? {
                continue;
            }
            if let Some(found) = self.check(value, &raw, &here, depth + 1)? {
                self.view
                    .set_field(&mut out, &name, found)
                    .map_err(Abort::View)?;
            }
        }
        Ok(if self.problems.len() > before {
            None
        } else {
            Some(self.view.close_map(out))
        })
    }

    /// Whether a `dict` key satisfies the key node's length and pattern;
    /// reports the first limit it breaks at `path` otherwise.
    fn map_key(
        &mut self,
        key: &Node<V::Rules>,
        name: &str,
        path: &str,
    ) -> Result<bool, Abort<V::Error>> {
        if name.contains('\0') {
            self.fail(
                path,
                "key_nul",
                "key must not contain NUL characters".to_owned(),
            )?;
            return Ok(false);
        }
        let size = i64::try_from(name.chars().count()).unwrap_or(i64::MAX);
        let plural = |bound: i64| if bound == 1 { "" } else { "s" };
        if let Some(least) = key.min_length.filter(|least| size < *least) {
            let message = format!("key must have at least {least} character{}", plural(least));
            self.fail(path, "key_min_length", message)?;
            return Ok(false);
        }
        if let Some(most) = self.most(key).filter(|most| size > *most) {
            let message = format!("key must have at most {most} character{}", plural(most));
            self.fail(path, "key_max_length", message)?;
            return Ok(false);
        }
        if let Some((source, regex)) = &key.pattern {
            if !regex.is_match(name) {
                let shown = self.view.pattern_shown(source).map_err(Abort::View)?;
                self.fail(path, "key_pattern", format!("key must match {shown}"))?;
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn schema(&mut self, index: usize, data: &V::Data, path: &str, depth: usize) -> Checked<V> {
        let Some(object) = self.view.object(data) else {
            return self.fail(path, "type", "expected an object".to_owned());
        };
        let definitions = self.definitions;
        let definition = &definitions[index];
        let before = self.problems.len();
        let use_aliases = self.options.use_aliases;
        let known: HashSet<&str> = definition
            .fields
            .iter()
            .map(|field| {
                if use_aliases {
                    field.wire_name.as_str()
                } else {
                    field.name.as_str()
                }
            })
            .collect();
        self.validate_object_key_types(&object, path)?;
        if self.problems.len() > before {
            return Ok(None);
        }
        let duplicate_names = if use_aliases {
            self.duplicate_python_names(definition, &object, path)?
        } else {
            HashSet::new()
        };
        if definition.forbid_extra {
            self.reject_unknown_fields(&object, &known, &duplicate_names, path)?;
        }
        let values = self.validate_fields(definition, &object, &duplicate_names, path, depth)?;
        if self.problems.len() > before {
            return Ok(None);
        }
        let construct = self.options.construct;
        let built = self
            .view
            .close_record(&definition.class, values, path, construct)
            .map_err(Abort::View)?;
        Ok(Some(built))
    }

    fn tagged_union(
        &mut self,
        discriminator: &str,
        wire_name: &str,
        variants: &[(String, usize)],
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let Some(object) = self.view.object(data) else {
            return self.fail(path, "type", "expected an object".to_owned());
        };
        let before = self.problems.len();
        self.validate_object_key_types(&object, path)?;
        if self.problems.len() > before {
            return Ok(None);
        }
        let use_aliases = self.options.use_aliases;
        let field_name = if use_aliases {
            wire_name
        } else {
            discriminator
        };
        let field_path = join(path, field_name);
        if use_aliases
            && wire_name != discriminator
            && self.both_present(&object, discriminator, wire_name)?
        {
            self.fail(
                &field_path,
                "duplicate_field",
                "field was supplied by both its Python name and alias".to_owned(),
            )?;
            return Ok(None);
        }
        let Some(tag) = self.discriminator_tag(&object, field_name, &field_path)? else {
            return Ok(None);
        };
        let Some((_, index)) = variants.iter().find(|(candidate, _)| *candidate == tag) else {
            self.fail(
                &field_path,
                "discriminator_unknown",
                "unknown discriminator value".to_owned(),
            )?;
            return Ok(None);
        };
        self.schema(*index, data, path, depth)
    }

    /// The discriminator's string value, or `None` after reporting why there
    /// is none.
    fn discriminator_tag(
        &mut self,
        object: &V::Object,
        field_name: &str,
        field_path: &str,
    ) -> Result<Option<String>, Abort<V::Error>> {
        let Some(raw_tag) = self.view.get(object, field_name).map_err(Abort::View)? else {
            self.fail(
                field_path,
                "discriminator_required",
                "discriminator field is required".to_owned(),
            )?;
            return Ok(None);
        };
        let Some(tag) = self.view.string(&raw_tag) else {
            self.fail(
                field_path,
                "discriminator_type",
                "discriminator must be a string".to_owned(),
            )?;
            return Ok(None);
        };
        Ok(Some(
            self.view.text(&tag).map_err(Abort::View)?.into_owned(),
        ))
    }

    fn both_present(
        &self,
        object: &V::Object,
        name: &str,
        wire_name: &str,
    ) -> Result<bool, Abort<V::Error>> {
        Ok(self.view.contains(object, name).map_err(Abort::View)?
            && self.view.contains(object, wire_name).map_err(Abort::View)?)
    }

    fn duplicate_python_names(
        &mut self,
        definition: &Definition<V::Rules>,
        object: &V::Object,
        path: &str,
    ) -> Result<HashSet<String>, Abort<V::Error>> {
        let mut duplicates = HashSet::new();
        for field in &definition.fields {
            if field.name != field.wire_name
                && self.both_present(object, &field.name, &field.wire_name)?
            {
                duplicates.insert(field.name.clone());
                self.fail(
                    &join(path, &field.wire_name),
                    "duplicate_field",
                    "field was supplied by both its Python name and alias".to_owned(),
                )?;
            }
        }
        Ok(duplicates)
    }

    fn reject_unknown_fields(
        &mut self,
        object: &V::Object,
        known: &HashSet<&str>,
        duplicate_names: &HashSet<String>,
        path: &str,
    ) -> Result<(), Abort<V::Error>> {
        for key in self.view.string_keys(object) {
            let name = self.view.key_text(&key).map_err(Abort::View)?.into_owned();
            let known_wire_name = known.contains(name.as_str());
            let duplicate_python_name = duplicate_names.contains(&name);
            if !known_wire_name && !duplicate_python_name {
                let shown: String = name.chars().take(MAX_KEY_ECHO).collect();
                self.fail(
                    &join(path, &shown),
                    "extra_forbidden",
                    "unknown field".to_owned(),
                )?;
            }
        }
        Ok(())
    }

    fn validate_object_key_types(
        &mut self,
        object: &V::Object,
        path: &str,
    ) -> Result<(), Abort<V::Error>> {
        for _ in 0..self.view.non_string_keys(object) {
            self.fail(path, "type", "object keys must be strings".to_owned())?;
        }
        Ok(())
    }

    fn validate_fields(
        &mut self,
        definition: &Definition<V::Rules>,
        object: &V::Object,
        duplicate_names: &HashSet<String>,
        path: &str,
        depth: usize,
    ) -> Result<V::Record, Abort<V::Error>> {
        let mut values = self.view.new_record();
        for field in &definition.fields {
            if path.is_empty() && self.options.skip_fields.contains(&field.name) {
                continue;
            }
            if duplicate_names.contains(&field.name) {
                continue;
            }
            let key = if self.options.use_aliases {
                &field.wire_name
            } else {
                &field.name
            };
            let here = join(path, key);
            match self.view.get(object, key).map_err(Abort::View)? {
                Some(raw) => {
                    if let Some(value) = self.check(&field.node, &raw, &here, depth + 1)? {
                        self.view
                            .set_field(&mut values, &field.name, value)
                            .map_err(Abort::View)?;
                    }
                }
                None if field.required => {
                    self.fail(&here, "required", "field is required".to_owned())?;
                }
                None => {}
            }
        }
        Ok(values)
    }
}

pub(super) fn is_canonical_uuid(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(at, b)| match at {
            8 | 13 | 18 | 23 => *b == b'-',
            _ => b.is_ascii_hexdigit(),
        })
}

/// `YYYY-MM-DDThh:mm:ss[.fraction][Z|+hh:mm]` (the offset is optional here; a
/// missing one is reported separately as `datetime_tz`).
pub(super) fn is_rfc3339(text: &str) -> bool {
    let bytes = text.as_bytes();
    let digits = |from: usize, count: usize| {
        bytes.len() >= from + count && bytes[from..from + count].iter().all(u8::is_ascii_digit)
    };
    let shape = digits(0, 4)
        && bytes.get(4) == Some(&b'-')
        && digits(5, 2)
        && bytes.get(7) == Some(&b'-')
        && digits(8, 2)
        && matches!(bytes.get(10), Some(b'T' | b't'))
        && digits(11, 2)
        && bytes.get(13) == Some(&b':')
        && digits(14, 2)
        && bytes.get(16) == Some(&b':')
        && digits(17, 2);
    if !shape {
        return false;
    }
    let mut at = 19;
    if bytes.get(at) == Some(&b'.') {
        let start = at + 1;
        at = start;
        while bytes.get(at).is_some_and(u8::is_ascii_digit) {
            at += 1;
        }
        if at == start {
            return false;
        }
    }
    match &bytes[at..] {
        [] | [b'Z' | b'z'] => true,
        [b'+' | b'-', ..] => {
            let rest = &bytes[at + 1..];
            rest.len() == 5
                && rest[..2].iter().all(u8::is_ascii_digit)
                && rest[2] == b':'
                && rest[3..].iter().all(u8::is_ascii_digit)
        }
        _ => false,
    }
}

// The GIL-free entry points have no caller in the extension yet: the native
// CRUD executor uses them. Allowing them keeps everything they reach checked.
#[cfg_attr(
    not(test),
    allow(dead_code, reason = "consumed by the native CRUD executor")
)]
impl Compiled {
    /// The GIL-free copy of this schema; `None` when a constraint has no
    /// exact plain-Rust equivalent.
    pub(crate) fn native(&self) -> Option<&NativeSchema> {
        self.native.as_deref()
    }

    pub(crate) fn native_shared(&self) -> Option<Arc<NativeSchema>> {
        self.native.as_ref().map(Arc::clone)
    }
}

// The GIL-free entry points have no caller in the extension yet: the native
// CRUD executor uses them. Allowing them keeps everything they reach checked.
#[cfg_attr(
    not(test),
    allow(dead_code, reason = "consumed by the native CRUD executor")
)]
impl NativeSchema {
    pub(crate) fn field_names(&self) -> Vec<String> {
        self.definitions[self.root]
            .fields
            .iter()
            .map(|field| field.name.clone())
            .collect()
    }

    /// Validate a decoded document without the GIL. `Ok` carries exactly
    /// the problems the Python path reports for the same input, or the
    /// value it would return with `construct=False`; `Err(Defer)` when that
    /// cannot be guaranteed. `options.construct` has no effect here.
    pub(crate) fn validate(
        &self,
        data: &Json,
        options: &Options,
    ) -> Result<(Option<Validated>, Vec<Problem>), Defer> {
        let view = JsonView { input: PhantomData };
        Run::new(view, &self.definitions, options).validate(self.root, &data)
    }
}

#[pymethods]
impl Compiled {
    /// The instance a JSON body describes, built in one pass from the bytes,
    /// or `None` when the ordinary decode and validate must run on them (a
    /// syntax error, a problem, or anything outside the typed subset).
    fn decode_typed<'py>(
        &self,
        py: Python<'py>,
        raw: &[u8],
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let Some(typed) = &self.typed else {
            return Ok(None);
        };
        typeddecode::decode(py, typed, raw)
    }

    /// The instance `data` describes, or a `ValidationError` listing every
    /// problem: `validate` without the tuple, for a schema that has no hook,
    /// check or normalizer to run around it.
    fn build<'py>(&self, py: Python<'py>, data: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.build_checked(py, data)
    }

    /// `build` for data that is all text (form fields, a parsed XML tree): the
    /// text is converted to the declared scalars first, in Rust.
    fn build_text<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let converted = self.coerce_text(py, data, true)?;
        self.build_checked(py, &converted)
    }

    /// A copy of `instance` with `changes` applied, or `None` when `replace`
    /// must run in Python (an unknown field, another class, no plan).
    fn replace<'py>(
        &self,
        instance: &Bound<'py, PyAny>,
        changes: &Bound<'py, PyDict>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        match &self.typed {
            Some(typed) => typeddecode::replaced(typed, instance, changes),
            None => Ok(None),
        }
    }

    /// The tree of a text source (form fields, query pairs, XML) with its text
    /// leaves converted to the scalar types this schema declares, ready for the
    /// strict `validate`. What does not convert is left for `validate` to refuse.
    #[pyo3(signature = (data, use_aliases=true))]
    fn coerce_text<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
        use_aliases: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        coerce::coerce_object(py, &self.definitions, self.root, data, use_aliases, 0)
    }

    /// `(instance, problems)`: exactly one of them is meaningful. Problems are
    /// `(path, code, message)` tuples in the order the checks found them.
    /// `input_bounds` (default: `construct`) applies the wire-input bounds of
    /// dict fields; construction and model conversion leave it off.
    #[pyo3(signature = (data, construct=true, use_aliases=true, allow_naive_datetime=false, skip_fields=Vec::new(), input_bounds=None))]
    #[expect(
        clippy::too_many_arguments,
        reason = "the keyword arguments of the Python method"
    )]
    pub(super) fn validate<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
        construct: bool,
        use_aliases: bool,
        allow_naive_datetime: bool,
        skip_fields: Vec<String>,
        input_bounds: Option<bool>,
    ) -> PyResult<(Option<Bound<'py, PyAny>>, Vec<Problem>)> {
        let options = Options {
            construct,
            use_aliases,
            allow_naive_datetime,
            skip_fields: skip_fields.into_iter().collect(),
            input_bounds: input_bounds.unwrap_or(construct),
        };
        let view = PyView {
            py,
            uuid: &self.uuid,
            from_iso: &self.from_iso,
            typed: self.typed.as_deref(),
        };
        Run::new(view, &self.definitions, &options).validate(self.root, data)
    }
}

impl Compiled {
    fn build_checked<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if let Some(typed) = self.typed.as_deref() {
            if let Some(built) = typeddecode::from_record(typed, data) {
                return Ok(built);
            }
        }
        let options = Options {
            construct: true,
            use_aliases: true,
            allow_naive_datetime: false,
            skip_fields: HashSet::new(),
            input_bounds: true,
        };
        let view = PyView {
            py,
            uuid: &self.uuid,
            from_iso: &self.from_iso,
            typed: self.typed.as_deref(),
        };
        let (instance, problems) =
            Run::new(view, &self.definitions, &options).validate(self.root, data)?;
        match instance {
            Some(instance) if problems.is_empty() => Ok(instance),
            _ => Err(validation_error(py, problems)?),
        }
    }
}

/// The `ValidationError` of `problems`, as `hinoki.errors` defines it.
fn validation_error(py: Python<'_>, problems: Vec<Problem>) -> PyResult<PyErr> {
    let errors = py.import("hinoki.errors")?;
    let detail = errors.getattr("ErrorDetail")?;
    let details = PyList::empty(py);
    for (path, code, message) in problems {
        details.append(detail.call1((path, code, message))?)?;
    }
    Ok(PyErr::from_value(
        errors.getattr("ValidationError")?.call1((details,))?,
    ))
}
