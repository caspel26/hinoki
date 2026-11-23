//! The values a template reads: what a context is made of.

use std::fmt::Write as _;

use super::error::Reason;
use super::limits::LINEAR_KEYS;

/// A map in insertion order. With more than `LINEAR_KEYS` keys a sorted index
/// is built when the map is made, so a lookup is a binary search.
#[derive(Clone, Debug, PartialEq)]
pub struct Map {
    entries: Vec<(String, Value)>,
    sorted: Vec<usize>,
}

impl Map {
    pub fn new(entries: Vec<(String, Value)>) -> Self {
        let mut sorted = Vec::new();
        if entries.len() > LINEAR_KEYS {
            sorted = (0..entries.len()).collect();
            sorted.sort_by(|a, b| entries[*a].0.cmp(&entries[*b].0));
        }
        Self { entries, sorted }
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        if self.sorted.is_empty() {
            return self
                .entries
                .iter()
                .find(|(name, _)| name == key)
                .map(|(_, value)| value);
        }
        self.sorted
            .binary_search_by(|index| self.entries[*index].0.as_str().cmp(key))
            .ok()
            .map(|found| &self.entries[self.sorted[found]].1)
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    List(Vec<Value>),
    Map(Map),
}

impl Value {
    pub fn map(entries: Vec<(String, Value)>) -> Value {
        Value::Map(Map::new(entries))
    }

    /// Whether an `if` takes the branch: false for null, false, zero, the
    /// empty text, list and map.
    pub fn truthy(&self) -> bool {
        match self {
            Value::Null => false,
            Value::Bool(flag) => *flag,
            Value::Int(number) => *number != 0,
            Value::Float(number) => *number != 0.0,
            Value::Str(text) => !text.is_empty(),
            Value::List(items) => !items.is_empty(),
            Value::Map(map) => !map.is_empty(),
        }
    }

    /// The text `{{ }}` writes for a scalar. A list or a map cannot be
    /// written; a float that is not finite is refused.
    pub(crate) fn scalar_text<'a>(&'a self, buffer: &'a mut String) -> Result<&'a str, Reason> {
        match self {
            Value::Null => Ok(""),
            Value::Bool(true) => Ok("true"),
            Value::Bool(false) => Ok("false"),
            Value::Str(text) => Ok(text),
            Value::Int(number) => {
                let _ = write!(buffer, "{number}");
                Ok(buffer)
            }
            Value::Float(number) if number.is_finite() => {
                let _ = write!(buffer, "{number}");
                if !buffer.contains('.') {
                    buffer.push_str(".0");
                }
                Ok(buffer)
            }
            Value::Float(_) => Err(Reason::UnsupportedValue),
            Value::List(_) | Value::Map(_) => Err(Reason::NotText),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(value: &Value) -> Result<String, Reason> {
        let mut buffer = String::new();
        value.scalar_text(&mut buffer).map(str::to_owned)
    }

    #[test]
    fn truthiness_follows_the_documented_table() {
        let falsy = [
            Value::Null,
            Value::Bool(false),
            Value::Int(0),
            Value::Float(0.0),
            Value::Float(-0.0),
            Value::Str(String::new()),
            Value::List(vec![]),
            Value::map(vec![]),
        ];
        for value in &falsy {
            assert!(!value.truthy(), "{value:?}");
        }
        let truthy = [
            Value::Bool(true),
            Value::Int(-1),
            Value::Float(0.5),
            Value::Str(" ".to_owned()),
            Value::Str("0".to_owned()),
            Value::List(vec![Value::Null]),
            Value::map(vec![("k".to_owned(), Value::Null)]),
        ];
        for value in &truthy {
            assert!(value.truthy(), "{value:?}");
        }
    }

    #[test]
    fn scalars_print_in_their_documented_form() {
        assert_eq!(text(&Value::Null).unwrap(), "");
        assert_eq!(text(&Value::Bool(true)).unwrap(), "true");
        assert_eq!(text(&Value::Bool(false)).unwrap(), "false");
        assert_eq!(text(&Value::Int(-3)).unwrap(), "-3");
        assert_eq!(text(&Value::Float(1.0)).unwrap(), "1.0");
        assert_eq!(text(&Value::Float(0.1)).unwrap(), "0.1");
        assert_eq!(text(&Value::Float(-2.5)).unwrap(), "-2.5");
        assert_eq!(
            text(&Value::Float(1e21)).unwrap(),
            "1000000000000000000000.0"
        );
        assert_eq!(text(&Value::Str("a<b".to_owned())).unwrap(), "a<b");
    }

    #[test]
    fn lists_maps_and_non_finite_floats_are_refused() {
        assert_eq!(text(&Value::List(vec![])), Err(Reason::NotText));
        assert_eq!(text(&Value::map(vec![])), Err(Reason::NotText));
        assert_eq!(text(&Value::Float(f64::NAN)), Err(Reason::UnsupportedValue));
        assert_eq!(
            text(&Value::Float(f64::INFINITY)),
            Err(Reason::UnsupportedValue)
        );
    }

    #[test]
    fn maps_find_keys_linearly_and_through_an_index_when_large() {
        let small = Map::new(vec![("b".to_owned(), Value::Int(2))]);
        assert_eq!(small.get("b"), Some(&Value::Int(2)));
        assert_eq!(small.get("a"), None);
        assert_eq!(small.len(), 1);
        let entries: Vec<_> = (0..40)
            .rev()
            .map(|i| (format!("k{i}"), Value::Int(i)))
            .collect();
        let large = Map::new(entries);
        for i in 0..40 {
            assert_eq!(large.get(&format!("k{i}")), Some(&Value::Int(i)));
        }
        assert_eq!(large.get("zz"), None);
    }
}
