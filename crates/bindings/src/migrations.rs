//! Migration planning, multi-dialect DDL generation, state validation, and SQL splitting.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PyString};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};

const MAX_IDENTIFIER: usize = 63;
const STATE_FORMAT_VERSION: i64 = 1;
const NOT_NULL_RISK: &str = "adding a NOT NULL column without a default fails when rows exist";

fn is_mysql_family(dialect: &str) -> bool {
    matches!(dialect, "mysql" | "mariadb")
}

fn identifier_limit(dialect: &str) -> usize {
    if is_mysql_family(dialect) {
        64
    } else {
        MAX_IDENTIFIER
    }
}

fn is_valid_identifier(name: &str, limit: usize) -> bool {
    let bytes = name.as_bytes();
    if bytes.is_empty() || bytes.len() > limit {
        return false;
    }
    let first = bytes[0];
    if !(first.is_ascii_alphabetic() || first == b'_') {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|b| b.is_ascii_alphanumeric() || *b == b'_')
}

fn py_repr_str(s: &str) -> String {
    let has_single = s.contains('\'');
    let has_double = s.contains('"');
    let quote = if has_single && !has_double { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
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
            c if c.is_control() => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn quote_ident(name: &str, dialect: &str) -> Result<String, String> {
    if !is_valid_identifier(name, identifier_limit(dialect)) {
        return Err(format!("invalid identifier {}", py_repr_str(name)));
    }
    if is_mysql_family(dialect) {
        Ok(format!("`{name}`"))
    } else {
        Ok(format!("\"{name}\""))
    }
}

fn on_delete_sql(rule: &str) -> Option<&'static str> {
    match rule {
        "restrict" => Some("RESTRICT"),
        "cascade" => Some("CASCADE"),
        "set_null" => Some("SET NULL"),
        "no_action" => Some("NO ACTION"),
        _ => None,
    }
}

fn dialect_type_str<'a>(state_type: &'a str, dialect: &str) -> &'a str {
    if dialect == "sqlite" {
        return match state_type {
            "uuid" => "UUID",
            "bigint" => "integer",
            "double precision" => "real",
            "bytea" => "blob",
            "numeric" => "text",
            other => other,
        };
    }
    if is_mysql_family(dialect) {
        return match state_type {
            "numeric" => "decimal(65,30)",
            "uuid" => "binary(16)",
            "double precision" => "double",
            "timestamptz" => "datetime(6)",
            "bytea" => "longblob",
            "jsonb" => "json",
            other => other,
        };
    }
    state_type
}

fn legacy_state_type(lower: &str) -> Option<&'static str> {
    match lower {
        "uuid" | "binary(16)" => Some("uuid"),
        "integer" => Some("bigint"),
        "real" | "double" => Some("double precision"),
        "blob" | "longblob" => Some("bytea"),
        "datetime(6)" => Some("timestamptz"),
        "json" => Some("jsonb"),
        _ => None,
    }
}

fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    let digest = hasher.finalize();
    let mut out = String::with_capacity(64);
    for b in digest {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

/// FIPS 180-4 SHA-1 used solely to derive deterministic temporary SQLite table names.
fn sha1_hex_8(data: &[u8]) -> String {
    let mut msg = data.to_vec();
    let bit_len = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while (msg.len() % 64) != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    for chunk in msg.as_chunks::<64>().0 {
        sha1_compress(&mut h, chunk);
    }
    format!("{:08x}", h[0])
}

fn sha1_compress(h: &mut [u32; 5], chunk: &[u8; 64]) {
    let mut w = [0u32; 80];
    for (slot, word) in w.iter_mut().zip(chunk.as_chunks::<4>().0) {
        *slot = u32::from_be_bytes(*word);
    }
    for i in 16..80 {
        w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
    }
    let [mut a, mut b, mut c, mut d, mut e] = *h;
    for (i, &wi) in w.iter().enumerate() {
        let (f, k) = sha1_round(i, b, c, d);
        let temp = a
            .rotate_left(5)
            .wrapping_add(f)
            .wrapping_add(e)
            .wrapping_add(k)
            .wrapping_add(wi);
        e = d;
        d = c;
        c = b.rotate_left(30);
        b = a;
        a = temp;
    }
    h[0] = h[0].wrapping_add(a);
    h[1] = h[1].wrapping_add(b);
    h[2] = h[2].wrapping_add(c);
    h[3] = h[3].wrapping_add(d);
    h[4] = h[4].wrapping_add(e);
}

fn sha1_round(i: usize, b: u32, c: u32, d: u32) -> (u32, u32) {
    match i {
        0..=19 => ((b & c) | ((!b) & d), 0x5A827999),
        20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
        40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
        _ => (b ^ c ^ d, 0xCA62C1D6),
    }
}

fn index_name_impl(table: &str, columns: &[String], dialect: &str) -> String {
    let limit = identifier_limit(dialect);
    let mut identity = format!("{}:{table}", table.chars().count());
    for c in columns {
        identity.push_str(&format!(":{}:{c}", c.chars().count()));
    }
    let digest = &sha256_hex(identity.as_bytes())[..8];
    let prefix = format!("ix_{table}_{}", columns.join("_"));
    let cut: String = prefix.chars().take(limit - 9).collect();
    format!("{cut}_{digest}")
}

fn foreign_key_name_impl(table: &str, column: &str, dialect: &str) -> String {
    let limit = identifier_limit(dialect);
    let tlen = table.chars().count();
    let clen = column.chars().count();
    let identity = format!("{tlen}:{table}:{clen}:{column}");
    let digest = &sha256_hex(identity.as_bytes())[..16];
    let prefix = format!("fk_{tlen}_{table}_{column}");
    let cut: String = prefix.chars().take(limit - 17).collect();
    format!("{cut}_{digest}")
}

fn type_family(sql_type: &str) -> &str {
    let name = sql_type.split('(').next().unwrap_or(sql_type);
    if name == "varchar" {
        "text"
    } else {
        name
    }
}

fn parse_obj(json_str: &str) -> Result<Map<String, Value>, String> {
    match serde_json::from_str::<Value>(json_str) {
        Ok(Value::Object(map)) => Ok(map),
        Ok(_) => Err("expected JSON object".to_string()),
        Err(e) => Err(format!("invalid JSON: {e}")),
    }
}

fn str_field<'a>(obj: &'a Map<String, Value>, key: &str) -> &'a str {
    obj.get(key).and_then(Value::as_str).unwrap_or("")
}

fn bool_field(obj: &Map<String, Value>, key: &str) -> bool {
    obj.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn obj_field<'a>(obj: &'a Map<String, Value>, key: &str) -> Option<&'a Map<String, Value>> {
    obj.get(key).and_then(Value::as_object)
}

fn string_groups(spec: &Map<String, Value>, key: &str) -> Vec<Vec<String>> {
    let Some(arr) = spec.get(key).and_then(Value::as_array) else {
        return Vec::new();
    };
    arr.iter()
        .filter_map(Value::as_array)
        .map(|g| {
            g.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .collect()
}

fn column_sql_impl(
    name: &str,
    column: &Map<String, Value>,
    dialect: &str,
    inline_references: bool,
    keys: bool,
) -> Result<String, String> {
    let mut parts = vec![
        quote_ident(name, dialect)?,
        dialect_type_str(str_field(column, "type"), dialect).to_string(),
    ];
    let pk = bool_field(column, "primary_key");
    let nullable = bool_field(column, "nullable");
    if pk && keys {
        parts.push("PRIMARY KEY".to_string());
    } else if !nullable || pk {
        parts.push("NOT NULL".to_string());
    }
    if bool_field(column, "unique") && !pk && keys {
        parts.push("UNIQUE".to_string());
    }
    if inline_references && keys {
        if let Some(target) = obj_field(column, "references") {
            let rule = on_delete_sql(str_field(target, "on_delete"))
                .ok_or_else(|| "invalid on_delete rule".to_string())?;
            let tt = quote_ident(str_field(target, "table"), dialect)?;
            let tc = quote_ident(str_field(target, "column"), dialect)?;
            parts.push(format!("REFERENCES {tt} ({tc}) ON DELETE {rule}"));
        }
    }
    Ok(parts.join(" "))
}

fn unique_constraint_sql_impl(
    group: &[String],
    spec: &Map<String, Value>,
    dialect: &str,
) -> Result<String, String> {
    let mut quoted = Vec::with_capacity(group.len());
    for c in group {
        quoted.push(quote_ident(c, dialect)?);
    }
    let columns = format!("UNIQUE ({})", quoted.join(", "));
    if let Some(unames) = obj_field(spec, "unique_names") {
        for (name, cols_val) in unames {
            if let Some(cols_arr) = cols_val.as_array() {
                let matches = cols_arr.len() == group.len()
                    && cols_arr
                        .iter()
                        .zip(group.iter())
                        .all(|(v, g)| v.as_str() == Some(g.as_str()));
                if matches {
                    return Ok(format!(
                        "CONSTRAINT {} {columns}",
                        quote_ident(name, dialect)?
                    ));
                }
            }
        }
    }
    Ok(columns)
}

fn foreign_key_sql_impl(
    table: &str,
    column: &str,
    state: &Map<String, Value>,
    dialect: &str,
) -> Result<String, String> {
    let Some(target) = obj_field(state, "references") else {
        return Err(format!("{table}.{column} is not a foreign key"));
    };
    let rule = on_delete_sql(str_field(target, "on_delete"))
        .ok_or_else(|| "invalid on_delete rule".to_string())?;
    let name = foreign_key_name_impl(table, column, dialect);
    Ok(format!(
        "CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}) ON DELETE {rule}",
        quote_ident(&name, dialect)?,
        quote_ident(column, dialect)?,
        quote_ident(str_field(target, "table"), dialect)?,
        quote_ident(str_field(target, "column"), dialect)?,
    ))
}

fn create_index_sql_impl(table: &str, columns: &[String], dialect: &str) -> Result<String, String> {
    let mut quoted = Vec::with_capacity(columns.len());
    for c in columns {
        quoted.push(quote_ident(c, dialect)?);
    }
    let idx = quote_ident(&index_name_impl(table, columns, dialect), dialect)?;
    let tbl = quote_ident(table, dialect)?;
    Ok(format!(
        "CREATE INDEX {idx} ON {tbl} ({});",
        quoted.join(", ")
    ))
}

fn create_table_sql_impl(
    name: &str,
    spec: &Map<String, Value>,
    dialect: &str,
) -> Result<String, String> {
    let mysql_family = is_mysql_family(dialect);
    let cols = obj_field(spec, "columns").ok_or_else(|| "missing columns".to_string())?;
    let mut lines = Vec::new();
    for (c, s) in cols {
        let s_obj = s.as_object().ok_or_else(|| "invalid column".to_string())?;
        lines.push(format!(
            "    {}",
            column_sql_impl(c, s_obj, dialect, !mysql_family, true)?
        ));
    }
    for group in string_groups(spec, "unique") {
        lines.push(format!(
            "    {}",
            unique_constraint_sql_impl(&group, spec, dialect)?
        ));
    }
    if mysql_family {
        for (c, s) in cols {
            if let Some(s_obj) = s.as_object() {
                if obj_field(s_obj, "references").is_some() {
                    lines.push(format!(
                        "    {}",
                        foreign_key_sql_impl(name, c, s_obj, dialect)?
                    ));
                }
            }
        }
    }
    let suffix = if mysql_family { " ENGINE=InnoDB" } else { "" };
    Ok(format!(
        "CREATE TABLE {} (\n{}\n){suffix};",
        quote_ident(name, dialect)?,
        lines.join(",\n")
    ))
}

fn keyed_columns(spec: &Map<String, Value>) -> HashSet<String> {
    let mut keyed = HashSet::new();
    if let Some(cols) = obj_field(spec, "columns") {
        for (name, val) in cols {
            if let Some(col) = val.as_object() {
                if bool_field(col, "unique")
                    || bool_field(col, "primary_key")
                    || obj_field(col, "references").is_some()
                {
                    keyed.insert(name.clone());
                }
            }
        }
    }
    for group in string_groups(spec, "unique") {
        keyed.extend(group);
    }
    for group in string_groups(spec, "indexes") {
        keyed.extend(group);
    }
    keyed
}

fn refuse_unbounded_keys(
    table: &str,
    spec: &Map<String, Value>,
    dialect: &str,
) -> Result<(), String> {
    if !is_mysql_family(dialect) {
        return Ok(());
    }
    let mut names: Vec<String> = keyed_columns(spec).into_iter().collect();
    names.sort();
    let Some(cols) = obj_field(spec, "columns") else {
        return Ok(());
    };
    for name in names {
        if let Some(col) = cols.get(&name).and_then(Value::as_object) {
            if str_field(col, "type") == "text" {
                return Err(format!(
                    "{table}.{name}: MySQL and MariaDB cannot key an unbounded text \
                     column; give the field a max_length"
                ));
            }
        }
    }
    Ok(())
}

fn creation_order_impl(tables: &Map<String, Value>) -> Result<Vec<String>, String> {
    let mut ordered: Vec<String> = Vec::with_capacity(tables.len());
    let mut ordered_set: HashSet<String> = HashSet::with_capacity(tables.len());
    let mut pending: Vec<&String> = tables.keys().collect();
    while !pending.is_empty() {
        let ready: Vec<String> = pending
            .iter()
            .filter(|&&name| table_is_ready(name, tables, &ordered_set))
            .map(|&name| name.clone())
            .collect();
        if ready.is_empty() {
            return Err("foreign keys form a cycle between tables".to_string());
        }
        for name in &ready {
            ordered.push(name.clone());
            ordered_set.insert(name.clone());
        }
        pending.retain(|name| !ordered_set.contains(name.as_str()));
    }
    Ok(ordered)
}

fn table_is_ready(name: &str, tables: &Map<String, Value>, ordered: &HashSet<String>) -> bool {
    let Some(cols) = tables
        .get(name)
        .and_then(Value::as_object)
        .and_then(|s| obj_field(s, "columns"))
    else {
        return true;
    };
    cols.values().all(|v| {
        let Some(target) = v.as_object().and_then(|c| obj_field(c, "references")) else {
            return true;
        };
        let target_table = str_field(target, "table");
        ordered.contains(target_table) || target_table == name || !tables.contains_key(target_table)
    })
}

fn check_references_impl(
    state: &Map<String, Value>,
    external: Option<&Map<String, Value>>,
) -> Result<(), String> {
    let Some(tables) = obj_field(state, "tables") else {
        return Ok(());
    };
    let ext_tables = external.and_then(|e| obj_field(e, "tables"));
    for (table, spec_val) in tables {
        let Some(cols) = spec_val.as_object().and_then(|s| obj_field(s, "columns")) else {
            continue;
        };
        for (name, col_val) in cols {
            let Some(col) = col_val.as_object() else {
                continue;
            };
            check_single_reference(table, name, col, tables, ext_tables)?;
        }
    }
    Ok(())
}

fn check_single_reference(
    table: &str,
    name: &str,
    col: &Map<String, Value>,
    tables: &Map<String, Value>,
    ext_tables: Option<&Map<String, Value>>,
) -> Result<(), String> {
    let Some(target) = obj_field(col, "references") else {
        return Ok(());
    };
    let target_table = str_field(target, "table");
    let target_col = str_field(target, "column");
    let remote = tables
        .get(target_table)
        .or_else(|| ext_tables.and_then(|e| e.get(target_table)))
        .and_then(Value::as_object)
        .and_then(|s| obj_field(s, "columns"))
        .and_then(|c| c.get(target_col))
        .and_then(Value::as_object);
    let Some(wanted) = remote else {
        return Err(format!("{table}.{name} references an unknown table"));
    };
    let col_type = str_field(col, "type");
    let wanted_type = str_field(wanted, "type");
    if type_family(wanted_type) != type_family(col_type) {
        return Err(format!(
            "{table}.{name} is {col_type} but {target_table}.{target_col} is {wanted_type}"
        ));
    }
    Ok(())
}

fn collect_generated_names(table: &str, spec: &Map<String, Value>) -> Vec<(String, String)> {
    let mut found = vec![(table.to_string(), format!("table {table}"))];
    for dialect in ["postgres", "mysql"] {
        for columns in string_groups(spec, "indexes") {
            found.push((
                index_name_impl(table, &columns, dialect),
                format!("an index of {table}"),
            ));
        }
        if let Some(cols) = obj_field(spec, "columns") {
            for (column, state) in cols {
                if state
                    .as_object()
                    .and_then(|c| obj_field(c, "references"))
                    .is_some()
                {
                    found.push((
                        foreign_key_name_impl(table, column, dialect),
                        format!("a foreign key of {table}"),
                    ));
                }
            }
        }
    }
    found
}

fn check_constraint_names_impl(tables: &Map<String, Value>) -> Result<(), String> {
    let mut taken: HashMap<String, String> = HashMap::new();
    for (table, spec_val) in tables {
        if let Some(spec) = spec_val.as_object() {
            for (name, owner) in collect_generated_names(table, spec) {
                taken.entry(name.to_lowercase()).or_insert(owner);
            }
        }
    }
    for (table, spec_val) in tables {
        let Some(unames) = spec_val
            .as_object()
            .and_then(|s| obj_field(s, "unique_names"))
        else {
            continue;
        };
        for name in unames.keys() {
            let folded = name.to_lowercase();
            if let Some(holder) = taken.get(&folded) {
                return Err(format!(
                    "{table}: unique constraint name {} is already used by \
                     {holder}; choose another name",
                    py_repr_str(name)
                ));
            }
            taken.insert(folded, format!("a unique constraint of {table}"));
        }
    }
    Ok(())
}

type OpTuple = (String, String, String);

fn unique_changed(old: &Map<String, Value>, new: &Map<String, Value>) -> bool {
    let empty_arr = Value::Array(Vec::new());
    let empty_obj = Value::Object(Map::new());
    let old_u = old.get("unique").unwrap_or(&empty_arr);
    let new_u = new.get("unique").unwrap_or(&empty_arr);
    let old_un = old.get("unique_names").unwrap_or(&empty_obj);
    let new_un = new.get("unique_names").unwrap_or(&empty_obj);
    old_u != new_u || old_un != new_un
}

fn index_changes_impl(
    table: &str,
    old: &Map<String, Value>,
    new: &Map<String, Value>,
    dialect: &str,
) -> Result<(Vec<OpTuple>, Vec<OpTuple>), String> {
    let before = string_groups(old, "indexes");
    let after = string_groups(new, "indexes");
    let mut created = Vec::new();
    for cols in &after {
        if !before.contains(cols) {
            created.push((
                format!("index {table} ({})", cols.join(", ")),
                create_index_sql_impl(table, cols, dialect)?,
                String::new(),
            ));
        }
    }
    let mut dropped = Vec::new();
    for cols in &before {
        if !after.contains(cols) {
            let idx = quote_ident(&index_name_impl(table, cols, dialect), dialect)?;
            let sql = if is_mysql_family(dialect) {
                format!("DROP INDEX {idx} ON {};", quote_ident(table, dialect)?)
            } else {
                format!("DROP INDEX {idx};")
            };
            dropped.push((
                format!("drop index {table} ({})", cols.join(", ")),
                sql,
                String::new(),
            ));
        }
    }
    Ok((created, dropped))
}

fn alter_column_impl(
    table: &str,
    name: &str,
    old: &Map<String, Value>,
    new: &Map<String, Value>,
    dialect: &str,
) -> Result<Vec<OpTuple>, String> {
    for key in ["primary_key", "unique", "references"] {
        if old.get(key) != new.get(key) {
            return Err(format!(
                "{table}.{name}: changing {key} is not planned automatically; \
                 write that migration by hand"
            ));
        }
    }
    if is_mysql_family(dialect) {
        let col = column_sql_impl(name, new, dialect, true, false)?;
        return Ok(vec![(
            format!("change {table}.{name}"),
            format!(
                "ALTER TABLE {} MODIFY COLUMN {col};",
                quote_ident(table, dialect)?
            ),
            "changing a column definition may rewrite the table or lose data".to_string(),
        )]);
    }
    let qt = quote_ident(table, dialect)?;
    let qn = quote_ident(name, dialect)?;
    let head = format!("ALTER TABLE {qt} ALTER COLUMN {qn}");
    let mut ops = Vec::new();
    let new_type = str_field(new, "type");
    if str_field(old, "type") != new_type {
        let dt = dialect_type_str(new_type, dialect);
        ops.push((
            format!("change {table}.{name} to {new_type}"),
            format!("{head} TYPE {dt} USING {qn}::{dt};"),
            "changing a column type can rewrite the table or lose data".to_string(),
        ));
    }
    let old_null = bool_field(old, "nullable");
    let new_null = bool_field(new, "nullable");
    if old_null && !new_null {
        ops.push((
            format!("require {table}.{name}"),
            format!("{head} SET NOT NULL;"),
            "fails when the column contains NULL".to_string(),
        ));
    }
    if !old_null && new_null {
        ops.push((
            format!("allow NULL in {table}.{name}"),
            format!("{head} DROP NOT NULL;"),
            String::new(),
        ));
    }
    Ok(ops)
}

fn add_column_impl(
    table: &str,
    name: &str,
    column: &Map<String, Value>,
    dialect: &str,
) -> Result<Vec<OpTuple>, String> {
    let mysql_family = is_mysql_family(dialect);
    let def = column_sql_impl(name, column, dialect, !mysql_family, true)?;
    let qt = quote_ident(table, dialect)?;
    let risk = if bool_field(column, "nullable") {
        ""
    } else {
        NOT_NULL_RISK
    };
    let mut ops = vec![(
        format!("add {table}.{name}"),
        format!("ALTER TABLE {qt} ADD COLUMN {def};"),
        risk.to_string(),
    )];
    if mysql_family && obj_field(column, "references").is_some() {
        let constraint = foreign_key_sql_impl(table, name, column, dialect)?;
        ops.push((
            format!("add foreign key {table}.{name}"),
            format!("ALTER TABLE {qt} ADD {constraint};"),
            String::new(),
        ));
    }
    Ok(ops)
}

fn removed_columns_impl(
    table: &str,
    old: &Map<String, Value>,
    new: &Map<String, Value>,
    dialect: &str,
) -> Result<Vec<OpTuple>, String> {
    let mut ops = Vec::new();
    let Some(old_cols) = obj_field(old, "columns") else {
        return Ok(ops);
    };
    let new_cols = obj_field(new, "columns");
    for (name, old_val) in old_cols {
        if new_cols.is_some_and(|m| m.contains_key(name)) {
            continue;
        }
        let has_ref = old_val
            .as_object()
            .and_then(|c| obj_field(c, "references"))
            .is_some();
        if is_mysql_family(dialect) && has_ref {
            return Err(format!(
                "{table}.{name}: dropping a foreign-key column is not planned \
                 automatically for MySQL/MariaDB; older Hinoki migrations may \
                 not have created the constraint, so write a reviewed migration"
            ));
        }
        ops.push((
            format!("drop {table}.{name}"),
            format!(
                "ALTER TABLE {} DROP COLUMN {};",
                quote_ident(table, dialect)?,
                quote_ident(name, dialect)?
            ),
            "drops the column and its data".to_string(),
        ));
    }
    Ok(ops)
}

fn sqlite_column_warnings(
    name: &str,
    previous: &Map<String, Value>,
    column: &Map<String, Value>,
) -> Vec<String> {
    let mut warnings = Vec::new();
    if previous == column {
        return warnings;
    }
    let new_type = str_field(column, "type");
    if str_field(previous, "type") != new_type {
        warnings.push(format!("changing {name} to {new_type} may convert values"));
    }
    if bool_field(previous, "nullable") && !bool_field(column, "nullable") {
        warnings.push(format!(
            "requiring {name} fails when existing rows contain NULL"
        ));
    }
    for key in ["primary_key", "unique", "references"] {
        if previous.get(key) != column.get(key) {
            warnings.push(format!("changing {key} on {name} may reject existing rows"));
        }
    }
    warnings
}

fn sqlite_new_column_warnings(
    old_cols: Option<&Map<String, Value>>,
    new_cols: &Map<String, Value>,
    destructive: &mut Vec<String>,
) {
    for (name, col_val) in new_cols {
        let Some(col) = col_val.as_object() else {
            continue;
        };
        match old_cols
            .and_then(|m| m.get(name))
            .and_then(Value::as_object)
        {
            None if !bool_field(col, "nullable") => {
                destructive.push(format!("adding {name} requires values for existing rows"));
            }
            None => {}
            Some(prev) => destructive.extend(sqlite_column_warnings(name, prev, col)),
        }
    }
}

fn sqlite_rebuild_warnings_impl(old: &Map<String, Value>, new: &Map<String, Value>) -> Vec<String> {
    let mut destructive = Vec::new();
    let old_cols = obj_field(old, "columns");
    let new_cols = obj_field(new, "columns");
    if let Some(nc) = new_cols {
        sqlite_new_column_warnings(old_cols, nc, &mut destructive);
    }
    if let Some(oc) = old_cols {
        for name in oc.keys() {
            if !new_cols.is_some_and(|m| m.contains_key(name)) {
                destructive.push(format!("dropping {name} loses its data"));
            }
        }
    }
    let empty_arr = Value::Array(Vec::new());
    if old.get("unique").unwrap_or(&empty_arr) != new.get("unique").unwrap_or(&empty_arr) {
        destructive
            .push("changing composite unique constraints may reject existing rows".to_string());
    }
    destructive
}

fn sqlite_rebuild_impl(
    table: &str,
    old: &Map<String, Value>,
    new: &Map<String, Value>,
    dialect: &str,
) -> Result<OpTuple, String> {
    let temporary = format!("__hinoki_{}", sha1_hex_8(table.as_bytes()));
    let mut stmts = vec![create_table_sql_impl(&temporary, new, dialect)?];
    let old_cols = obj_field(old, "columns");
    let new_cols = obj_field(new, "columns");
    let common: Vec<String> = old_cols
        .map(|oc| {
            oc.keys()
                .filter(|k| new_cols.is_some_and(|nc| nc.contains_key(*k)))
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    let destructive = sqlite_rebuild_warnings_impl(old, new);
    let qt = quote_ident(table, dialect)?;
    let qtmp = quote_ident(&temporary, dialect)?;
    if !common.is_empty() {
        let mut quoted = Vec::with_capacity(common.len());
        for c in &common {
            quoted.push(quote_ident(c, dialect)?);
        }
        let cols_sql = quoted.join(", ");
        stmts.push(format!(
            "INSERT INTO {qtmp} ({cols_sql}) SELECT {cols_sql} FROM {qt};"
        ));
    }
    stmts.push(format!("DROP TABLE {qt};"));
    stmts.push(format!("ALTER TABLE {qtmp} RENAME TO {qt};"));
    for cols in string_groups(new, "indexes") {
        stmts.push(create_index_sql_impl(table, &cols, dialect)?);
    }
    Ok((
        format!("rebuild table {table}"),
        stmts.join("\n"),
        destructive.join("; "),
    ))
}

fn stored_columns(spec: &Map<String, Value>, dialect: &str) -> Map<String, Value> {
    let mut out = Map::new();
    let Some(cols) = obj_field(spec, "columns") else {
        return out;
    };
    for (name, val) in cols {
        if let Some(col) = val.as_object() {
            let mut copy = col.clone();
            let dt = dialect_type_str(str_field(col, "type"), dialect).to_string();
            copy.insert("type".to_string(), Value::String(dt));
            out.insert(name.clone(), Value::Object(copy));
        }
    }
    out
}

fn table_changes_impl(
    table: &str,
    old: &Map<String, Value>,
    new: &Map<String, Value>,
    dialect: &str,
) -> Result<Vec<OpTuple>, String> {
    if dialect == "sqlite" {
        if stored_columns(old, dialect) != stored_columns(new, dialect) || unique_changed(old, new)
        {
            return Ok(vec![sqlite_rebuild_impl(table, old, new, dialect)?]);
        }
        let (created, dropped) = index_changes_impl(table, old, new, dialect)?;
        return Ok([dropped, created].concat());
    }
    refuse_unbounded_keys(table, new, dialect)?;
    if unique_changed(old, new) {
        return Err(format!(
            "{table}: changing composite unique constraints is not planned \
             automatically; write that migration by hand"
        ));
    }
    let (created, dropped) = index_changes_impl(table, old, new, dialect)?;
    let mut ops = dropped;
    let old_cols = obj_field(old, "columns");
    if let Some(new_cols) = obj_field(new, "columns") {
        for (name, col_val) in new_cols {
            let Some(col) = col_val.as_object() else {
                continue;
            };
            match old_cols
                .and_then(|m| m.get(name))
                .and_then(Value::as_object)
            {
                None => ops.extend(add_column_impl(table, name, col, dialect)?),
                Some(old_col) if old_col != col => {
                    ops.extend(alter_column_impl(table, name, old_col, col, dialect)?)
                }
                Some(_) => {}
            }
        }
    }
    ops.extend(created);
    ops.extend(removed_columns_impl(table, old, new, dialect)?);
    Ok(ops)
}

fn create_table_op(
    name: &str,
    spec: &Map<String, Value>,
    dialect: &str,
) -> Result<OpTuple, String> {
    refuse_unbounded_keys(name, spec, dialect)?;
    let mut stmt = create_table_sql_impl(name, spec, dialect)?;
    for cols in string_groups(spec, "indexes") {
        stmt.push('\n');
        stmt.push_str(&create_index_sql_impl(name, &cols, dialect)?);
    }
    Ok((format!("create table {name}"), stmt, String::new()))
}

fn diff_impl(
    old: &Map<String, Value>,
    new: &Map<String, Value>,
    dialect: &str,
) -> Result<Vec<OpTuple>, String> {
    let empty = Map::new();
    let old_tables = obj_field(old, "tables").unwrap_or(&empty);
    let new_tables = obj_field(new, "tables").unwrap_or(&empty);
    let mut ops = Vec::new();
    for name in creation_order_impl(new_tables)? {
        if !old_tables.contains_key(&name) {
            if let Some(spec) = new_tables.get(&name).and_then(Value::as_object) {
                ops.push(create_table_op(&name, spec, dialect)?);
            }
        }
    }
    for (name, spec_val) in new_tables {
        if let (Some(old_spec), Some(new_spec)) = (
            old_tables.get(name).and_then(Value::as_object),
            spec_val.as_object(),
        ) {
            ops.extend(table_changes_impl(name, old_spec, new_spec, dialect)?);
        }
    }
    for name in creation_order_impl(old_tables)?.into_iter().rev() {
        if !new_tables.contains_key(&name) {
            ops.push((
                format!("drop table {name}"),
                format!("DROP TABLE {};", quote_ident(&name, dialect)?),
                "drops the table and all its rows".to_string(),
            ));
        }
    }
    Ok(ops)
}

fn is_py_line_break(ch: char) -> bool {
    matches!(
        ch,
        '\n' | '\r'
            | '\x0b'
            | '\x0c'
            | '\x1c'
            | '\x1d'
            | '\x1e'
            | '\u{0085}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

fn py_splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut iter = text.char_indices().peekable();
    while let Some((idx, ch)) = iter.next() {
        if !is_py_line_break(ch) {
            continue;
        }
        lines.push(&text[start..idx]);
        let mut next_start = idx + ch.len_utf8();
        if ch == '\r' && iter.peek().is_some_and(|&(_, next_ch)| next_ch == '\n') {
            iter.next();
            next_start += 1;
        }
        start = next_start;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

fn statements_impl(text: &str) -> Result<Vec<String>, String> {
    let mut found = Vec::new();
    let mut current: Vec<&str> = Vec::new();
    for line in py_splitlines(text) {
        if current.is_empty() && (line.trim().is_empty() || line.trim_start().starts_with("--")) {
            continue;
        }
        current.push(line);
        if line.trim_end().ends_with(';') {
            let joined = current.join("\n");
            let trimmed = joined.trim_end();
            found.push(trimmed.strip_suffix(';').unwrap_or(trimmed).to_string());
            current.clear();
        }
    }
    if !current.is_empty() {
        return Err("the last statement does not end with ';'".to_string());
    }
    Ok(found)
}

fn auto_name_impl(description: &str) -> String {
    let mut slug = String::with_capacity(description.len());
    let mut prev_dash = true;
    for ch in description.chars() {
        let lower = ch.to_ascii_lowercase();
        if lower.is_ascii_lowercase() || lower.is_ascii_digit() {
            slug.push(lower);
            prev_dash = false;
        } else if !prev_dash {
            slug.push('-');
            prev_dash = true;
        }
    }
    let trimmed = slug.trim_matches('-');
    let cut: String = trimmed.chars().take(80).collect();
    let final_slug = cut.trim_end_matches('-');
    if final_slug.is_empty() {
        "schema-update".to_string()
    } else {
        final_slug.to_string()
    }
}

fn validate_state_version(state: &Bound<'_, PyDict>, filename: &str) -> PyResult<()> {
    let Some(version) = state.get_item("format_version")? else {
        return Ok(());
    };
    let valid = version.is_exact_instance_of::<PyInt>()
        && version.extract::<i64>().ok() == Some(STATE_FORMAT_VERSION);
    if !valid {
        let repr = version.repr()?.to_str()?.to_string();
        return Err(PyValueError::new_err(format!(
            "{filename} uses unsupported migration state format version {repr}"
        )));
    }
    Ok(())
}

fn validate_constraint_groups(
    filename: &str,
    table_repr: &str,
    spec: &Bound<'_, PyDict>,
    key: &str,
) -> PyResult<()> {
    let err = || PyValueError::new_err(format!("{filename} has invalid {key} for {table_repr}"));
    let Some(groups_obj) = spec.get_item(key)? else {
        return Err(err());
    };
    let Ok(groups) = groups_obj.cast::<PyList>() else {
        return Err(err());
    };
    for group_obj in groups.iter() {
        let Ok(group) = group_obj.cast::<PyList>() else {
            return Err(err());
        };
        if group.is_empty() || group.iter().any(|c| !c.is_instance_of::<PyString>()) {
            return Err(err());
        }
    }
    Ok(())
}

fn validate_column_reference(
    filename: &str,
    table: &str,
    name: &str,
    reference: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if reference.is_none() {
        return Ok(());
    }
    let err = || {
        PyValueError::new_err(format!(
            "{filename} has invalid column state for {table}.{name}"
        ))
    };
    let Ok(ref_dict) = reference.cast::<PyDict>() else {
        return Err(err());
    };
    let has_str = |k: &str| -> PyResult<bool> {
        Ok(ref_dict
            .get_item(k)?
            .is_some_and(|v| v.is_instance_of::<PyString>()))
    };
    if !has_str("table")? || !has_str("column")? || !has_str("on_delete")? {
        return Err(err());
    }
    let on_delete = ref_dict
        .get_item("on_delete")?
        .unwrap()
        .extract::<String>()?;
    if on_delete_sql(&on_delete).is_none() {
        return Err(err());
    }
    Ok(())
}

fn validate_migration_column(
    filename: &str,
    table: &str,
    name_obj: &Bound<'_, PyAny>,
    column_obj: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let name_str = name_obj.str()?.to_str()?.to_string();
    let err = || {
        PyValueError::new_err(format!(
            "{filename} has invalid column state for {table}.{name_str}"
        ))
    };
    if !name_obj.is_instance_of::<PyString>() {
        return Err(err());
    }
    let Ok(col) = column_obj.cast::<PyDict>() else {
        return Err(err());
    };
    let is_str = |k: &str| -> PyResult<bool> {
        Ok(col
            .get_item(k)?
            .is_some_and(|v| v.is_instance_of::<PyString>()))
    };
    let is_bool = |k: &str| -> PyResult<bool> {
        Ok(col
            .get_item(k)?
            .is_some_and(|v| v.is_instance_of::<PyBool>()))
    };
    if !is_str("type")? || !is_bool("nullable")? || !is_bool("primary_key")? || !is_bool("unique")?
    {
        return Err(err());
    }
    let Some(reference) = col.get_item("references")? else {
        return Err(err());
    };
    validate_column_reference(filename, table, &name_str, &reference)
}

fn validate_migration_table(
    py: Python<'_>,
    filename: &str,
    table_obj: &Bound<'_, PyAny>,
    spec_obj: &Bound<'_, PyAny>,
    legacy_format: bool,
) -> PyResult<()> {
    let Ok(table_py) = table_obj.cast::<PyString>() else {
        return Err(PyValueError::new_err(format!(
            "{filename} has an invalid table entry"
        )));
    };
    let Ok(spec) = spec_obj.cast::<PyDict>() else {
        return Err(PyValueError::new_err(format!(
            "{filename} has an invalid table entry"
        )));
    };
    let table_repr = table_py.repr()?.to_str()?.to_string();
    let Some(cols_obj) = spec.get_item("columns")? else {
        return Err(PyValueError::new_err(format!(
            "{filename} has invalid columns for {table_repr}"
        )));
    };
    let Ok(cols) = cols_obj.cast::<PyDict>() else {
        return Err(PyValueError::new_err(format!(
            "{filename} has invalid columns for {table_repr}"
        )));
    };
    if legacy_format && !spec.contains("indexes")? {
        spec.set_item("indexes", PyList::empty(py))?;
    }
    validate_constraint_groups(filename, &table_repr, spec, "unique")?;
    validate_constraint_groups(filename, &table_repr, spec, "indexes")?;
    let table_str = table_py.to_str()?;
    for (name_obj, col_obj) in cols.iter() {
        validate_migration_column(filename, table_str, &name_obj, &col_obj)?;
    }
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (name, dialect = "postgres"))]
pub fn migration_quote(name: &str, dialect: &str) -> PyResult<String> {
    quote_ident(name, dialect).map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (table, columns, dialect = "postgres"))]
pub fn migration_index_name(table: &str, columns: Vec<String>, dialect: &str) -> String {
    index_name_impl(table, &columns, dialect)
}

#[pyfunction]
#[pyo3(signature = (table, column, dialect = "postgres"))]
pub fn migration_foreign_key_name(table: &str, column: &str, dialect: &str) -> String {
    foreign_key_name_impl(table, column, dialect)
}

#[pyfunction]
pub fn migration_dialect_type(state_type: &str, dialect: &str) -> String {
    dialect_type_str(state_type, dialect).to_string()
}

#[pyfunction]
pub fn migration_creation_order(tables_json: &str) -> PyResult<Vec<String>> {
    let tables = parse_obj(tables_json).map_err(PyValueError::new_err)?;
    creation_order_impl(&tables).map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (state_json, external_json = None))]
pub fn migration_check_references(state_json: &str, external_json: Option<&str>) -> PyResult<()> {
    let state = parse_obj(state_json).map_err(PyValueError::new_err)?;
    let external = external_json
        .map(parse_obj)
        .transpose()
        .map_err(PyValueError::new_err)?;
    check_references_impl(&state, external.as_ref()).map_err(PyValueError::new_err)
}

#[pyfunction]
pub fn migration_check_constraint_names(tables_json: &str) -> PyResult<()> {
    let tables = parse_obj(tables_json).map_err(PyValueError::new_err)?;
    check_constraint_names_impl(&tables).map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (name, spec_json, dialect = "postgres"))]
pub fn migration_create_table_sql(name: &str, spec_json: &str, dialect: &str) -> PyResult<String> {
    let spec = parse_obj(spec_json).map_err(PyValueError::new_err)?;
    create_table_sql_impl(name, &spec, dialect).map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (table, column, state_json, dialect = "postgres"))]
pub fn migration_foreign_key_sql(
    table: &str,
    column: &str,
    state_json: &str,
    dialect: &str,
) -> PyResult<String> {
    let state = parse_obj(state_json).map_err(PyValueError::new_err)?;
    foreign_key_sql_impl(table, column, &state, dialect).map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (table, name, old_json, new_json, dialect = "postgres"))]
pub fn migration_alter_column(
    table: &str,
    name: &str,
    old_json: &str,
    new_json: &str,
    dialect: &str,
) -> PyResult<Vec<OpTuple>> {
    let old = parse_obj(old_json).map_err(PyValueError::new_err)?;
    let new = parse_obj(new_json).map_err(PyValueError::new_err)?;
    alter_column_impl(table, name, &old, &new, dialect).map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (table, old_json, new_json, dialect = "postgres"))]
pub fn migration_table_changes(
    table: &str,
    old_json: &str,
    new_json: &str,
    dialect: &str,
) -> PyResult<Vec<OpTuple>> {
    let old = parse_obj(old_json).map_err(PyValueError::new_err)?;
    let new = parse_obj(new_json).map_err(PyValueError::new_err)?;
    table_changes_impl(table, &old, &new, dialect).map_err(PyValueError::new_err)
}

#[pyfunction]
pub fn migration_sqlite_rebuild_warnings(old_json: &str, new_json: &str) -> PyResult<Vec<String>> {
    let old = parse_obj(old_json).map_err(PyValueError::new_err)?;
    let new = parse_obj(new_json).map_err(PyValueError::new_err)?;
    Ok(sqlite_rebuild_warnings_impl(&old, &new))
}

#[pyfunction]
#[pyo3(signature = (old_json, new_json, dialect = "postgres"))]
pub fn migration_diff(old_json: &str, new_json: &str, dialect: &str) -> PyResult<Vec<OpTuple>> {
    let old = parse_obj(old_json).map_err(PyValueError::new_err)?;
    let new = parse_obj(new_json).map_err(PyValueError::new_err)?;
    diff_impl(&old, &new, dialect).map_err(PyValueError::new_err)
}

#[pyfunction]
pub fn migration_validate_state<'py>(
    py: Python<'py>,
    state: &Bound<'py, PyAny>,
    filename: &str,
) -> PyResult<Py<PyAny>> {
    let Ok(state_dict) = state.cast::<PyDict>() else {
        return Err(PyValueError::new_err(format!(
            "{filename} must contain a tables object"
        )));
    };
    let legacy_format = !state_dict.contains("format_version")?;
    validate_state_version(state_dict, filename)?;
    let Some(tables_obj) = state_dict.get_item("tables")? else {
        return Err(PyValueError::new_err(format!(
            "{filename} must contain a tables object"
        )));
    };
    let Ok(tables) = tables_obj.cast::<PyDict>() else {
        return Err(PyValueError::new_err(format!(
            "{filename} must contain a tables object"
        )));
    };
    for (table_obj, spec_obj) in tables.iter() {
        validate_migration_table(py, filename, &table_obj, &spec_obj, legacy_format)?;
    }
    for (_, spec_obj) in tables.iter() {
        let cols_obj = spec_obj.cast::<PyDict>()?.get_item("columns")?.unwrap();
        for (_, col_obj) in cols_obj.cast::<PyDict>()?.iter() {
            let col = col_obj.cast::<PyDict>()?;
            let typ = col.get_item("type")?.unwrap().extract::<String>()?;
            if let Some(norm) = legacy_state_type(&typ.to_lowercase()) {
                col.set_item("type", norm)?;
            }
        }
    }
    Ok(state.clone().unbind())
}

#[pyfunction]
pub fn migration_statements(text: &str) -> PyResult<Vec<String>> {
    statements_impl(text).map_err(PyValueError::new_err)
}

#[pyfunction]
pub fn migration_render(title: &str, operations: Vec<OpTuple>) -> String {
    let mut lines = vec![
        format!("-- hinoki migration {title}"),
        "-- generated from the models; review it.".to_string(),
        String::new(),
    ];
    for (description, sql, destructive) in operations {
        lines.push(format!("-- {description}"));
        if !destructive.is_empty() {
            lines.push(format!("-- DESTRUCTIVE: {destructive}"));
        }
        lines.push(sql);
        lines.push(String::new());
    }
    lines.join("\n")
}

#[pyfunction]
pub fn migration_auto_name(description: &str) -> String {
    auto_name_impl(description)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(migration_quote, m)?)?;
    m.add_function(wrap_pyfunction!(migration_index_name, m)?)?;
    m.add_function(wrap_pyfunction!(migration_foreign_key_name, m)?)?;
    m.add_function(wrap_pyfunction!(migration_dialect_type, m)?)?;
    m.add_function(wrap_pyfunction!(migration_creation_order, m)?)?;
    m.add_function(wrap_pyfunction!(migration_check_references, m)?)?;
    m.add_function(wrap_pyfunction!(migration_check_constraint_names, m)?)?;
    m.add_function(wrap_pyfunction!(migration_create_table_sql, m)?)?;
    m.add_function(wrap_pyfunction!(migration_foreign_key_sql, m)?)?;
    m.add_function(wrap_pyfunction!(migration_alter_column, m)?)?;
    m.add_function(wrap_pyfunction!(migration_table_changes, m)?)?;
    m.add_function(wrap_pyfunction!(migration_sqlite_rebuild_warnings, m)?)?;
    m.add_function(wrap_pyfunction!(migration_diff, m)?)?;
    m.add_function(wrap_pyfunction!(migration_validate_state, m)?)?;
    m.add_function(wrap_pyfunction!(migration_statements, m)?)?;
    m.add_function(wrap_pyfunction!(migration_render, m)?)?;
    m.add_function(wrap_pyfunction!(migration_auto_name, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha1_matches_standard_vectors() {
        assert_eq!(sha1_hex_8(b""), "da39a3ee");
        assert_eq!(sha1_hex_8(b"abc"), "a9993e36");
        assert_eq!(sha1_hex_8(b"users"), "5b7dcd14");
    }

    #[test]
    fn statements_splits_and_validates() {
        let sql = "-- header\n\nCREATE TABLE a (\n  id int\n);\n-- mid\nDROP TABLE a;\n";
        let got = statements_impl(sql).unwrap();
        assert_eq!(got, vec!["CREATE TABLE a (\n  id int\n)", "DROP TABLE a"]);
        assert_eq!(
            statements_impl("SELECT 1;\u{2028}SELECT 2;\x0cSELECT 3;").unwrap(),
            vec!["SELECT 1", "SELECT 2", "SELECT 3"]
        );
        assert!(statements_impl("SELECT 1").is_err());
    }

    #[test]
    fn auto_name_matches_slug_rules() {
        assert_eq!(
            auto_name_impl("create table User_Posts!"),
            "create-table-user-posts"
        );
        assert_eq!(auto_name_impl("!!!"), "schema-update");
    }
}
