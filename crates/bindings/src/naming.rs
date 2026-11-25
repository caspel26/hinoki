//! Deterministic model naming, English pluralization, app label extraction,
//! and derived SQL table validation (`hinoki.orm.naming`).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const MIGRATION_LEDGER: &str = "hinoki_migrations";

fn is_word_boundary(chars: &[char], idx: usize) -> bool {
    let prev = chars[idx - 1];
    let cur = chars[idx];
    let next = chars.get(idx + 1).copied();
    let lower_or_digit_to_upper = (prev.is_lowercase() || prev.is_numeric()) && cur.is_uppercase();
    let acronym_to_word =
        prev.is_uppercase() && cur.is_uppercase() && next.is_some_and(char::is_lowercase);
    lower_or_digit_to_upper || acronym_to_word
}

fn split_words(name: &str) -> Vec<String> {
    let chars: Vec<char> = name.chars().collect();
    let mut words = Vec::new();
    let mut current = String::new();
    for (idx, &ch) in chars.iter().enumerate() {
        if ch == '_' {
            if !current.is_empty() {
                words.push(std::mem::take(&mut current));
            }
            continue;
        }
        if !current.is_empty() && is_word_boundary(&chars, idx) {
            words.push(std::mem::take(&mut current));
        }
        current.push(ch);
    }
    if !current.is_empty() {
        words.push(current);
    }
    words
}

fn is_all_upper(word: &str) -> bool {
    let mut has_cased = false;
    for c in word.chars() {
        if c.is_lowercase() {
            return false;
        }
        if c.is_uppercase() {
            has_cased = true;
        }
    }
    has_cased
}

fn plural_word(word: &str, acronym: bool) -> String {
    let ascii_alpha = !word.is_empty() && word.bytes().all(|b| b.is_ascii_alphabetic());
    if acronym && ascii_alpha && word.bytes().all(|b| b.is_ascii_uppercase()) {
        return format!("{word}s");
    }
    if ascii_alpha {
        let lower = word.to_ascii_lowercase();
        let bytes = lower.as_bytes();
        if bytes.len() > 1
            && bytes[bytes.len() - 1] == b'y'
            && !matches!(bytes[bytes.len() - 2], b'a' | b'e' | b'i' | b'o' | b'u')
        {
            return format!("{}ies", &word[..word.len() - 1]);
        }
        if lower.ends_with("ch")
            || lower.ends_with("sh")
            || matches!(bytes[bytes.len() - 1], b's' | b'x' | b'z')
        {
            return format!("{word}es");
        }
    }
    format!("{word}s")
}

fn human_singular(words: &[String]) -> String {
    let mut rendered: Vec<String> = words
        .iter()
        .map(|w| {
            if is_all_upper(w) {
                w.clone()
            } else {
                w.to_lowercase()
            }
        })
        .collect();
    if let (Some(first), Some(orig)) = (rendered.first_mut(), words.first()) {
        if !is_all_upper(orig) {
            let mut chars = first.chars();
            if let Some(c) = chars.next() {
                let mut cap: String = c.to_uppercase().collect();
                cap.push_str(chars.as_str());
                *first = cap;
            }
        }
    }
    rendered.join(" ")
}

fn human_plural(label: &str) -> String {
    match label.rsplit_once(' ') {
        Some((head, tail)) => {
            let plural = plural_word(tail, is_all_upper(tail));
            format!("{head} {plural}")
        }
        None => plural_word(label, is_all_upper(label)),
    }
}

fn is_valid_identifier(table: &str) -> bool {
    let bytes = table.as_bytes();
    if bytes.is_empty() || bytes.len() > 63 {
        return false;
    }
    let first_ok = bytes[0].is_ascii_alphabetic() || bytes[0] == b'_';
    first_ok
        && bytes[1..]
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || *b == b'_')
}

fn is_valid_app_label(app: &str) -> bool {
    let bytes = app.as_bytes();
    if bytes.is_empty() || !bytes[0].is_ascii_lowercase() {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
}

fn derived_table_problem(class_name: &str, table: &str) -> String {
    let lowered = table.to_lowercase();
    let reason = if !is_valid_identifier(table) {
        "is not a portable SQL identifier"
    } else if lowered == MIGRATION_LEDGER {
        "is owned by Hinoki migrations"
    } else if lowered.starts_with("sqlite_") {
        "uses SQLite's reserved sqlite_ prefix"
    } else {
        return String::new();
    };
    format!(
        "{class_name}: derived table '{table}' {reason}; name the table \
         with @db.table(...) or table="
    )
}

fn derived_table(class_name: &str, words: &[String], app: &str) -> (String, String) {
    if words.is_empty() || app.is_empty() {
        return (
            String::new(),
            format!(
                "{class_name}: cannot derive a table name from this class name; \
                 name the table with @db.table(...) or table="
            ),
        );
    }
    let mut parts: Vec<String> = Vec::with_capacity(words.len() + 1);
    parts.push(app.to_owned());
    for word in words {
        parts.push(word.to_lowercase());
    }
    if let Some(last) = parts.last_mut() {
        *last = plural_word(last, false);
    }
    let table = parts.join("_");
    let problem = derived_table_problem(class_name, &table);
    (table, problem)
}

fn check_display_name(class_name: &str, option: &str, value: &str) -> Result<(), String> {
    if value.trim().is_empty() || value.chars().any(char::is_control) {
        return Err(format!(
            "{class_name}: {option}= must be non-empty and contain no control characters"
        ));
    }
    Ok(())
}

#[pyfunction]
pub fn snake_case(name: &str) -> String {
    split_words(name)
        .into_iter()
        .map(|w| w.to_lowercase())
        .collect::<Vec<_>>()
        .join("_")
}

#[pyfunction]
pub fn app_label(module: &str) -> String {
    let parts: Vec<&str> = module.split('.').filter(|p| !p.is_empty()).collect();
    if parts.is_empty() {
        return String::new();
    }
    let pos = parts.iter().position(|p| p.eq_ignore_ascii_case("models"));
    let chosen = match pos {
        Some(idx) if idx > 0 => parts[idx - 1],
        _ => parts[parts.len() - 1],
    };
    let replaced: String = chosen
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect();
    replaced.trim_matches('_').to_lowercase()
}

#[pyfunction]
#[pyo3(signature = (class_name, table, label, plural, abstract_model, module="", app=""))]
pub fn resolve_model_names(
    class_name: &str,
    table: &str,
    label: &str,
    plural: &str,
    abstract_model: bool,
    module: &str,
    app: &str,
) -> PyResult<(String, String, String, bool, String)> {
    if !app.is_empty() && !is_valid_app_label(app) {
        return Err(PyValueError::new_err(format!(
            "{class_name}: app='{app}' must be lowercase letters, digits and \
             underscores, starting with a letter"
        )));
    }
    if abstract_model && !table.is_empty() {
        return Err(PyValueError::new_err(format!(
            "{class_name}: an abstract model has no table; remove table=... \
             or abstract=True"
        )));
    }
    let words = split_words(class_name);
    if words.is_empty() && (label.is_empty() || plural.is_empty()) {
        return Err(PyValueError::new_err(format!(
            "{class_name}: cannot derive names from this class name; pass \
             label= and plural= explicitly, and name the table with \
             @db.table(...) or table="
        )));
    }
    let singular = if label.is_empty() {
        human_singular(&words)
    } else {
        label.to_owned()
    };
    let plural_name = if plural.is_empty() {
        human_plural(&singular)
    } else {
        plural.to_owned()
    };
    check_display_name(class_name, "label", &singular).map_err(PyValueError::new_err)?;
    check_display_name(class_name, "plural", &plural_name).map_err(PyValueError::new_err)?;
    if abstract_model {
        return Ok((String::new(), singular, plural_name, false, String::new()));
    }
    if !table.is_empty() {
        return Ok((
            table.to_owned(),
            singular,
            plural_name,
            false,
            String::new(),
        ));
    }
    let effective_app = if app.is_empty() {
        app_label(module)
    } else {
        app.to_owned()
    };
    let (derived, problem) = derived_table(class_name, &words, &effective_app);
    Ok((derived, singular, plural_name, true, problem))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn converts_camel_and_acronyms_to_snake_case() {
        assert_eq!(snake_case("PostTag"), "post_tag");
        assert_eq!(snake_case("HTTPResponse"), "http_response");
        assert_eq!(snake_case("API2Key"), "api2_key");
        assert_eq!(snake_case("___"), "");
    }

    #[test]
    fn extracts_app_labels() {
        assert_eq!(app_label("shop.apps.billing.models"), "billing");
        assert_eq!(app_label("shop.models.invoice"), "shop");
        assert_eq!(app_label("standalone"), "standalone");
        assert_eq!(app_label(""), "");
    }

    #[test]
    fn pluralizes_words_and_acronyms() {
        assert_eq!(plural_word("Category", false), "Categories");
        assert_eq!(plural_word("Day", false), "Days");
        assert_eq!(plural_word("Box", false), "Boxes");
        assert_eq!(plural_word("API", true), "APIs");
    }
}
