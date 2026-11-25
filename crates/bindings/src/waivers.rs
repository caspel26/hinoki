//! Waiver validation and case-sensitive glob matching (`hinoki-rules.toml`).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyString};

const REQUIRED: [&str; 4] = ["rule", "target", "reason", "owner"];
const MIN_PREFIX: usize = 5;

static DATE_TYPES: PyOnceLock<(Py<PyAny>, Py<PyAny>)> = PyOnceLock::new();

fn date_types(py: Python<'_>) -> PyResult<(&Bound<'_, PyAny>, &Bound<'_, PyAny>)> {
    let pair = DATE_TYPES.get_or_try_init(py, || {
        let dt = py.import("datetime")?;
        Ok::<_, PyErr>((
            dt.getattr("date")?.unbind(),
            dt.getattr("datetime")?.unbind(),
        ))
    })?;
    Ok((pair.0.bind(py), pair.1.bind(py)))
}

fn bracket_contains(inner: &[char], ch: char) -> bool {
    let mut k = 0;
    while k < inner.len() {
        if k + 2 < inner.len() && inner[k + 1] == '-' {
            if inner[k] <= ch && ch <= inner[k + 2] {
                return true;
            }
            k += 3;
        } else {
            if inner[k] == ch {
                return true;
            }
            k += 1;
        }
    }
    false
}

/// Match a character class `[...]` starting right after `[`.
/// Returns `Some((matched, next_pattern_index))` or `None` if unclosed.
fn match_bracket(pat: &[char], p: usize, ch: char) -> Option<(bool, usize)> {
    let negate = matches!(pat.get(p), Some('!'));
    let start = p + usize::from(negate);
    let scan = start + usize::from(matches!(pat.get(start), Some(']')));
    let rel = pat.get(scan..)?.iter().position(|&c| c == ']')?;
    let end = scan + rel;
    let matched = bracket_contains(&pat[start..end], ch);
    Some((if negate { !matched } else { matched }, end + 1))
}

fn step_atom(pat: &[char], p: usize, ch: char) -> Option<usize> {
    match pat.get(p) {
        Some('?') => Some(p + 1),
        Some('[') => match match_bracket(pat, p + 1, ch) {
            Some((true, next_p)) => Some(next_p),
            Some((false, _)) => None,
            None => (ch == '[').then_some(p + 1),
        },
        Some(&c) if c == ch => Some(p + 1),
        _ => None,
    }
}

fn fnmatch_slice(text: &[char], pat: &[char]) -> bool {
    let (mut t, mut p) = (0, 0);
    let mut star: Option<(usize, usize)> = None;
    while t < text.len() {
        if pat.get(p) == Some(&'*') {
            p += 1;
            star = Some((p, t));
        } else if let Some(next_p) = step_atom(pat, p, text[t]) {
            p = next_p;
            t += 1;
        } else if let Some((sp, st)) = star {
            p = sp;
            t = st + 1;
            star = Some((sp, t));
        } else {
            return false;
        }
    }
    while pat.get(p) == Some(&'*') {
        p += 1;
    }
    p == pat.len()
}

#[pyfunction]
pub fn fnmatch_case(text: &str, pattern: &str) -> bool {
    let t: Vec<char> = text.chars().collect();
    let p: Vec<char> = pattern.chars().collect();
    fnmatch_slice(&t, &p)
}

fn is_bounded_target(target: &str) -> bool {
    let prefix = target
        .split_once(['*', '?', '['])
        .map_or(target, |(head, _)| head);
    prefix == target || (prefix.contains('.') && prefix.chars().count() >= MIN_PREFIX)
}

fn check_required_fields(index: usize, raw: &Bound<'_, PyDict>) -> PyResult<[String; 4]> {
    let mut wrong = Vec::new();
    let mut missing = Vec::new();
    let mut vals = [String::new(), String::new(), String::new(), String::new()];
    for (slot, &key) in REQUIRED.iter().enumerate() {
        if let Some(val) = raw.get_item(key)? {
            if !val.is_instance_of::<PyString>() {
                wrong.push(key);
            } else {
                let s: String = val.extract()?;
                let trimmed = s.trim();
                if trimmed.is_empty() {
                    missing.push(key);
                } else {
                    vals[slot] = trimmed.to_owned();
                }
            }
        } else {
            missing.push(key);
        }
    }
    if !wrong.is_empty() {
        return Err(PyValueError::new_err(format!(
            "waiver #{index}: {} must be text",
            wrong.join(", ")
        )));
    }
    if !missing.is_empty() {
        return Err(PyValueError::new_err(format!(
            "waiver #{index}: missing {}",
            missing.join(", ")
        )));
    }
    Ok(vals)
}

fn check_unknown_keys(index: usize, raw: &Bound<'_, PyDict>) -> PyResult<()> {
    let mut unknown = Vec::new();
    for k in raw.keys() {
        let is_known = k
            .extract::<String>()
            .is_ok_and(|name| REQUIRED.contains(&name.as_str()) || name == "expires");
        if !is_known {
            unknown.push(k.repr()?.to_str()?.to_owned());
        }
    }
    if !unknown.is_empty() {
        unknown.sort();
        return Err(PyValueError::new_err(format!(
            "waiver #{index}: unknown keys [{}]",
            unknown.join(", ")
        )));
    }
    Ok(())
}

type ValidatedWaiver = (String, String, String, String, Option<Py<PyAny>>);

#[pyfunction]
pub fn validate_waiver(
    py: Python<'_>,
    index: usize,
    raw: &Bound<'_, PyDict>,
) -> PyResult<ValidatedWaiver> {
    let [rule, target, reason, owner] = check_required_fields(index, raw)?;
    check_unknown_keys(index, raw)?;
    if !is_bounded_target(&target) {
        let target_repr = PyString::new(py, &target).repr()?.to_str()?.to_owned();
        return Err(PyValueError::new_err(format!(
            "waiver #{index}: the target {target_repr} is not bounded; start it \
             with a literal module or action prefix of at least {MIN_PREFIX} \
             characters that contains a '.' (for example 'shop.api.*')"
        )));
    }
    let expires = match raw.get_item("expires")? {
        Some(val) if !val.is_none() => {
            let (date_cls, datetime_cls) = date_types(py)?;
            if !val.is_instance(date_cls)? {
                return Err(PyValueError::new_err(format!(
                    "waiver #{index}: expires must be a date such as 2027-01-31"
                )));
            }
            if val.is_instance(datetime_cls)? {
                return Err(PyValueError::new_err(format!(
                    "waiver #{index}: expires must be a date, not a time"
                )));
            }
            Some(val.unbind())
        }
        _ => None,
    };
    Ok((rule, target, reason, owner, expires))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fnmatch_and_target_bounds() {
        assert!(fnmatch_case("blog.api.health", "blog.api.*"));
        assert!(!fnmatch_case("Blog.api.health", "blog.api.*"));
        assert!(fnmatch_case("a1b", "a[0-9]b"));
        assert!(!fnmatch_case("axb", "a[0-9]b"));
        assert!(fnmatch_case("axb", "a[!0-9]b"));
        assert!(fnmatch_case("a]b", "a[]]b"));
        assert!(fnmatch_case("a[b", "a[b"));
        assert!(is_bounded_target("shop.api.*"));
        assert!(!is_bounded_target("*"));
        assert!(!is_bounded_target("a.b.*"));
        assert!(is_bounded_target("exact"));
    }
}
