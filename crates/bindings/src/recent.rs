//! Rotating JSONL request recorder and tail reader for `hinoki dev`.

use std::collections::VecDeque;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3::types::PyList;
use serde_json::Value;

use crate::db::json_value_to_py;

fn rotated_path(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".1");
    path.with_file_name(name)
}

#[pyfunction]
pub fn append_recent(path: &str, batch: Vec<String>, max_bytes: u64) -> PyResult<()> {
    let target = Path::new(path);
    if let Some(parent) = target.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::create_dir_all(parent).map_err(|e| PyOSError::new_err(e.to_string()))?;
    }
    {
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(target)
            .map_err(|e| PyOSError::new_err(e.to_string()))?;
        let mut buf = batch.join("\n");
        buf.push('\n');
        file.write_all(buf.as_bytes())
            .map_err(|e| PyOSError::new_err(e.to_string()))?;
    }
    let size = fs::metadata(target)
        .map_err(|e| PyOSError::new_err(e.to_string()))?
        .len();
    if size > max_bytes {
        fs::rename(target, rotated_path(target)).map_err(|e| PyOSError::new_err(e.to_string()))?;
    }
    Ok(())
}

fn push_file_records(file: &Path, limit: usize, records: &mut VecDeque<Value>) {
    if !file.is_file() {
        return;
    }
    let Ok(bytes) = fs::read(file) else {
        return;
    };
    let text = String::from_utf8_lossy(&bytes);
    for line in text.lines() {
        if let Ok(val @ Value::Object(_)) = serde_json::from_str::<Value>(line) {
            if records.len() == limit {
                records.pop_front();
            }
            records.push_back(val);
        }
    }
}

#[pyfunction]
pub fn read_recent<'py>(py: Python<'py>, path: &str, limit: isize) -> PyResult<Bound<'py, PyList>> {
    if limit <= 0 {
        return Ok(PyList::empty(py));
    }
    let cap = limit as usize;
    let target = Path::new(path);
    let mut records = VecDeque::with_capacity(cap.min(1024));
    push_file_records(&rotated_path(target), cap, &mut records);
    push_file_records(target, cap, &mut records);
    let out = PyList::empty(py);
    for rec in records {
        out.append(json_value_to_py(py, &rec)?)?;
    }
    Ok(out)
}
