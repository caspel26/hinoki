//! Breaking-change diff engine between two OpenAPI 3.x documents.

use std::collections::{HashMap, HashSet};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::{Map, Value};

const METHODS: [&str; 9] = [
    "get", "put", "post", "delete", "options", "head", "patch", "trace", "query",
];
const JSON_MEDIA: &str = "application/json";

fn py_quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => py_quote(s),
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", parts.join(", "))
        }
        Value::Object(map) => {
            let parts: Vec<String> = map
                .iter()
                .map(|(k, v)| format!("{}: {}", py_quote(k), py_repr(v)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
    }
}

fn numbers_eq(a: &serde_json::Number, b: &serde_json::Number) -> bool {
    if a == b {
        return true;
    }
    if let (Some(ai), Some(bi)) = (a.as_i64(), b.as_i64()) {
        return ai == bi;
    }
    if let (Some(au), Some(bu)) = (a.as_u64(), b.as_u64()) {
        return au == bu;
    }
    match (a.as_f64(), b.as_f64()) {
        (Some(af), Some(bf)) => af == bf,
        _ => false,
    }
}

fn json_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Null, Value::Null) => true,
        (Value::Bool(x), Value::Bool(y)) => x == y,
        (Value::Number(x), Value::Number(y)) => numbers_eq(x, y),
        (Value::String(x), Value::String(y)) => x == y,
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y.iter()).all(|(l, r)| json_eq(l, r))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(k, v)| y.get(k).is_some_and(|yv| json_eq(v, yv)))
        }
        _ => false,
    }
}

fn py_repr_list(items: &[&Value]) -> String {
    let parts: Vec<String> = items.iter().map(|v| py_repr(v)).collect();
    format!("[{}]", parts.join(", "))
}

fn operation_of<'a>(item: &'a Map<String, Value>, method: &str) -> Option<&'a Map<String, Value>> {
    let found = item.get(method).or_else(|| {
        if method == "query" {
            item.get("x-query")
        } else {
            None
        }
    });
    found.and_then(Value::as_object)
}

fn body_schema(body: Option<&Value>) -> Option<&Map<String, Value>> {
    let obj = body?.as_object()?;
    if obj.is_empty() {
        return None;
    }
    obj.get("content")?
        .as_object()?
        .get(JSON_MEDIA)?
        .as_object()?
        .get("schema")?
        .as_object()
}

fn kind(value: &Value) -> String {
    match value {
        Value::Array(items) => items
            .iter()
            .map(|v| v.as_str().map_or_else(|| v.to_string(), str::to_owned))
            .collect::<Vec<_>>()
            .join("/"),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn is_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => false,
        Some(Value::Bool(true)) => true,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(m)) => !m.is_empty(),
    }
}

fn resolve_ref<'a>(
    schema: &'a Map<String, Value>,
    doc: &'a Value,
    empty: &'a Map<String, Value>,
) -> &'a Map<String, Value> {
    let Some(ref_str) = schema.get("$ref").and_then(Value::as_str) else {
        return schema;
    };
    let Some(path) = ref_str.strip_prefix("#/") else {
        return schema;
    };
    let mut cur = doc;
    for part in path.split('/') {
        let Some(next) = cur.as_object().and_then(|m| m.get(part)) else {
            return empty;
        };
        cur = next;
    }
    cur.as_object().unwrap_or(empty)
}

struct Comparison<'a> {
    old: &'a Value,
    new: &'a Value,
    out: Vec<String>,
    seen: HashSet<(String, String)>,
}

impl<'a> Comparison<'a> {
    fn new(old: &'a Value, new: &'a Value) -> Self {
        Self {
            old,
            new,
            out: Vec::new(),
            seen: HashSet::new(),
        }
    }

    fn operation(&mut self, label: &str, old: &Map<String, Value>, new: &Map<String, Value>) {
        self.request(label, old, new);
        self.responses(label, old.get("responses"), new.get("responses"));
        self.parameters(label, old.get("parameters"), new.get("parameters"));
    }

    fn request(&mut self, label: &str, old: &Map<String, Value>, new: &Map<String, Value>) {
        let before = body_schema(old.get("requestBody"));
        let after = body_schema(new.get("requestBody"));
        match (before, after) {
            (Some(b), Some(a)) => {
                self.seen.clear();
                self.schema(&format!("{label} request body"), b, a, true);
            }
            (None, Some(_))
                if is_truthy(
                    new.get("requestBody")
                        .and_then(Value::as_object)
                        .and_then(|m| m.get("required")),
                ) =>
            {
                self.out.push(format!("{label}: new required request body"));
            }
            _ => {}
        }
    }

    fn responses(&mut self, label: &str, old: Option<&Value>, new: Option<&Value>) {
        let Some(old_map) = old.and_then(Value::as_object) else {
            return;
        };
        let new_map = new.and_then(Value::as_object);
        for (status, resp) in old_map {
            let Some(after_resp) = new_map.and_then(|m| m.get(status)) else {
                if status.starts_with('2') {
                    self.out.push(format!("{label}: removed response {status}"));
                }
                continue;
            };
            let before = body_schema(Some(resp));
            let after = body_schema(Some(after_resp));
            match (before, after) {
                (Some(b), Some(a)) => {
                    self.seen.clear();
                    self.schema(&format!("{label} response {status}"), b, a, false);
                }
                (Some(_), None) if status.starts_with('2') => {
                    self.out
                        .push(format!("{label}: response {status} dropped its JSON body"));
                }
                _ => {}
            }
        }
    }

    fn compare_parameter(
        &mut self,
        label: &str,
        known: &HashMap<(&str, &str), &Map<String, Value>>,
        param: &Map<String, Value>,
    ) {
        let (Some(loc), Some(name)) = (
            param.get("in").and_then(Value::as_str),
            param.get("name").and_then(Value::as_str),
        ) else {
            return;
        };
        let qname = py_quote(name);
        let where_s = format!("{label}: {loc} parameter {qname}");
        let Some(&before) = known.get(&(loc, name)) else {
            if is_truthy(param.get("required")) {
                self.out
                    .push(format!("{label}: new required {loc} parameter {qname}"));
            }
            return;
        };
        if is_truthy(param.get("required")) && !is_truthy(before.get("required")) {
            self.out.push(format!("{where_s} became required"));
        }
        if let (Some(bs), Some(as_)) = (
            before.get("schema").and_then(Value::as_object),
            param.get("schema").and_then(Value::as_object),
        ) {
            self.seen.clear();
            self.schema(&where_s, bs, as_, true);
        }
    }

    fn parameters(&mut self, label: &str, old: Option<&Value>, new: Option<&Value>) {
        let mut known: HashMap<(&str, &str), &Map<String, Value>> = HashMap::new();
        if let Some(old_list) = old.and_then(Value::as_array) {
            for item in old_list.iter().filter_map(Value::as_object) {
                if let (Some(loc), Some(name)) = (
                    item.get("in").and_then(Value::as_str),
                    item.get("name").and_then(Value::as_str),
                ) {
                    known.insert((loc, name), item);
                }
            }
        }
        if let Some(new_list) = new.and_then(Value::as_array) {
            for param in new_list.iter().filter_map(Value::as_object) {
                self.compare_parameter(label, &known, param);
            }
        }
    }

    fn schema(
        &mut self,
        where_s: &str,
        old: &Map<String, Value>,
        new: &Map<String, Value>,
        incoming: bool,
    ) {
        let empty = Map::new();
        let before = resolve_ref(old, self.old, &empty);
        let after = resolve_ref(new, self.new, &empty);
        let ref_a = old.get("$ref").and_then(Value::as_str).unwrap_or("");
        let ref_b = new.get("$ref").and_then(Value::as_str).unwrap_or("");
        if !ref_a.is_empty() || !ref_b.is_empty() {
            let pair = (ref_a.to_owned(), ref_b.to_owned());
            if !self.seen.insert(pair) {
                return;
            }
        }
        if self.type_changed(where_s, before, after) {
            return;
        }
        self.check_enum(where_s, before, after, incoming);
        self.properties(where_s, before, after, incoming);
        if let (Some(bi), Some(ai)) = (
            before.get("items").and_then(Value::as_object),
            after.get("items").and_then(Value::as_object),
        ) {
            self.schema(&format!("{where_s}[]"), bi, ai, incoming);
        }
    }

    fn type_changed(
        &mut self,
        where_s: &str,
        before: &Map<String, Value>,
        after: &Map<String, Value>,
    ) -> bool {
        if let (Some(bt), Some(at)) = (before.get("type"), after.get("type")) {
            if bt != at {
                self.out.push(format!(
                    "{where_s}: type changed from {} to {}",
                    kind(bt),
                    kind(at)
                ));
                return true;
            }
        }
        false
    }

    fn check_enum(
        &mut self,
        where_s: &str,
        before: &Map<String, Value>,
        after: &Map<String, Value>,
        incoming: bool,
    ) {
        let (Some(be), Some(ae)) = (
            before.get("enum").and_then(Value::as_array),
            after.get("enum").and_then(Value::as_array),
        ) else {
            return;
        };
        if incoming {
            let lost: Vec<&Value> = be
                .iter()
                .filter(|v| !ae.iter().any(|av| json_eq(v, av)))
                .collect();
            if !lost.is_empty() {
                self.out.push(format!(
                    "{where_s}: enum lost values {}",
                    py_repr_list(&lost)
                ));
            }
        } else {
            let gained: Vec<&Value> = ae
                .iter()
                .filter(|v| !be.iter().any(|bv| json_eq(v, bv)))
                .collect();
            if !gained.is_empty() {
                self.out.push(format!(
                    "{where_s}: enum gained values {}",
                    py_repr_list(&gained)
                ));
            }
        }
    }

    fn properties(
        &mut self,
        where_s: &str,
        before: &Map<String, Value>,
        after: &Map<String, Value>,
        incoming: bool,
    ) {
        let old_props = before.get("properties").and_then(Value::as_object);
        let new_props = after.get("properties").and_then(Value::as_object);
        if incoming {
            self.required(where_s, old_props, before, after);
        } else if let Some(op) = old_props {
            for name in op.keys() {
                if !new_props.is_some_and(|np| np.contains_key(name)) {
                    self.out
                        .push(format!("{where_s}: removed property {}", py_quote(name)));
                }
            }
        }
        if let (Some(op), Some(np)) = (old_props, new_props) {
            for (name, old_schema) in op {
                if let (Some(os), Some(ns)) = (
                    old_schema.as_object(),
                    np.get(name).and_then(Value::as_object),
                ) {
                    self.schema(&format!("{where_s}.{name}"), os, ns, incoming);
                }
            }
        }
    }

    fn required(
        &mut self,
        where_s: &str,
        known: Option<&Map<String, Value>>,
        before: &Map<String, Value>,
        after: &Map<String, Value>,
    ) {
        let was: HashSet<&str> = before
            .get("required")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        let Some(after_req) = after.get("required").and_then(Value::as_array) else {
            return;
        };
        for name in after_req.iter().filter_map(Value::as_str) {
            if was.contains(name) {
                continue;
            }
            let q = py_quote(name);
            if known.is_some_and(|k| k.contains_key(name)) {
                self.out
                    .push(format!("{where_s}: property {q} became required"));
            } else {
                self.out
                    .push(format!("{where_s}: new required property {q}"));
            }
        }
    }
}

#[pyfunction]
pub fn openapi_breaking_changes(old_json: &[u8], new_json: &[u8]) -> PyResult<Vec<String>> {
    let old: Value =
        serde_json::from_slice(old_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let new: Value =
        serde_json::from_slice(new_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let mut cmp = Comparison::new(&old, &new);
    let Some(old_paths) = old.get("paths").and_then(Value::as_object) else {
        return Ok(cmp.out);
    };
    let new_paths = new.get("paths").and_then(Value::as_object);
    for (path, item_val) in old_paths {
        let Some(item) = item_val.as_object() else {
            continue;
        };
        let new_item = new_paths
            .and_then(|p| p.get(path))
            .and_then(Value::as_object);
        for method in METHODS {
            let Some(before) = operation_of(item, method) else {
                continue;
            };
            let label = format!("{} {path}", method.to_ascii_uppercase());
            let after = new_item.and_then(|ni| operation_of(ni, method));
            match after {
                None => cmp.out.push(format!("removed operation {label}")),
                Some(a) => cmp.operation(&label, before, a),
            }
        }
    }
    Ok(cmp.out)
}
