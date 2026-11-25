//! Bounded in-memory file storage and storage key/content-type validation.

use std::collections::HashMap;
use std::sync::Mutex;

use pyo3::exceptions::{PyRuntimeError, PyUnicodeEncodeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use sha2::{Digest, Sha256};

const MAX_KEY_BYTES: usize = 128;
const MAX_CONTENT_TYPE_BYTES: usize = 255;

fn extract_utf8_opt(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    match obj.extract::<String>() {
        Ok(s) => Ok(Some(s)),
        Err(err) if err.is_instance_of::<PyUnicodeEncodeError>(py) => Ok(None),
        Err(err) => Err(err),
    }
}

fn is_key_byte(b: u8) -> bool {
    matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'/' | b'_' | b'-')
}

fn is_media_part_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric()
        || matches!(
            b,
            b'!' | b'#' | b'$' | b'&' | b'^' | b'_' | b'.' | b'+' | b'-'
        )
}

pub(crate) fn is_valid_storage_key(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= MAX_KEY_BYTES
        && !key.starts_with('/')
        && key.bytes().all(is_key_byte)
        && !key.split('/').any(|part| part == "..")
}

#[pyfunction]
pub fn validate_storage_key(py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
    let Some(text) = extract_utf8_opt(py, key)? else {
        return Ok(false);
    };
    Ok(is_valid_storage_key(&text))
}

fn normalize_content_type_str(value: &str) -> String {
    if !value.is_ascii() || value.len() > MAX_CONTENT_TYPE_BYTES {
        return "application/octet-stream".to_owned();
    }
    let Some((kind, subtype)) = value.split_once('/') else {
        return "application/octet-stream".to_owned();
    };
    if kind.is_empty()
        || subtype.is_empty()
        || !kind.bytes().all(is_media_part_byte)
        || !subtype.bytes().all(is_media_part_byte)
    {
        return "application/octet-stream".to_owned();
    }
    value.to_ascii_lowercase()
}

#[pyfunction]
pub fn normalize_content_type(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<String> {
    let Some(text) = extract_utf8_opt(py, value)? else {
        return Ok("application/octet-stream".to_owned());
    };
    Ok(normalize_content_type_str(&text))
}

fn make_storage_key() -> String {
    let token = uuid::Uuid::new_v4().simple().to_string();
    format!("{}/{}/{}", &token[..2], &token[2..4], token)
}

#[pyfunction]
pub fn new_storage_key() -> String {
    make_storage_key()
}

fn sha256_hex_bytes(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    let mut out = String::with_capacity(64);
    for b in digest {
        use std::fmt::Write as _;
        let _ = write!(&mut out, "{b:02x}");
    }
    out
}

struct StoredBlob {
    key: String,
    size: usize,
    content_type: String,
    sha256: String,
    filename: String,
    content: Vec<u8>,
}

type StoredMeta = (String, usize, String, String, String);

#[derive(Default)]
struct State {
    items: HashMap<String, StoredBlob>,
    total_size: usize,
}

#[pyclass(name = "NativeMemoryStorage", module = "hinoki.core")]
pub struct NativeMemoryStorage {
    max_file_bytes: usize,
    max_total_bytes: usize,
    state: Mutex<State>,
}

impl NativeMemoryStorage {
    fn lock(&self) -> PyResult<std::sync::MutexGuard<'_, State>> {
        self.state
            .lock()
            .map_err(|_| PyRuntimeError::new_err("storage state lock is poisoned"))
    }

    fn effective_limit(&self, max_bytes: Option<usize>) -> usize {
        match max_bytes {
            Some(v) => v.min(self.max_file_bytes),
            None => self.max_file_bytes,
        }
    }
}

#[pymethods]
impl NativeMemoryStorage {
    #[new]
    fn new(max_file_bytes: usize, max_total_bytes: usize) -> Self {
        Self {
            max_file_bytes,
            max_total_bytes,
            state: Mutex::new(State::default()),
        }
    }

    #[pyo3(signature = (content, content_type, filename, max_bytes=None))]
    fn save(
        &self,
        py: Python<'_>,
        content: &[u8],
        content_type: &Bound<'_, PyAny>,
        filename: &str,
        max_bytes: Option<usize>,
    ) -> PyResult<StoredMeta> {
        let limit = self.effective_limit(max_bytes);
        if content.len() > limit {
            return Err(PyValueError::new_err("file_too_large"));
        }
        let media = match extract_utf8_opt(py, content_type)? {
            Some(s) => normalize_content_type_str(&s),
            None => "application/octet-stream".to_owned(),
        };
        let key = make_storage_key();
        let sha256 = sha256_hex_bytes(content);
        let size = content.len();
        let mut guard = self.lock()?;
        if guard.total_size.saturating_add(size) > self.max_total_bytes {
            return Err(PyValueError::new_err("quota_exceeded"));
        }
        let meta = (
            key.clone(),
            size,
            media.clone(),
            sha256.clone(),
            filename.to_owned(),
        );
        guard.items.insert(
            key.clone(),
            StoredBlob {
                key,
                size,
                content_type: media,
                sha256,
                filename: filename.to_owned(),
                content: content.to_vec(),
            },
        );
        guard.total_size += size;
        Ok(meta)
    }

    #[pyo3(signature = (key, max_bytes=None))]
    fn read<'py>(
        &self,
        py: Python<'py>,
        key: &Bound<'_, PyAny>,
        max_bytes: Option<usize>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let Some(key) = extract_utf8_opt(py, key)?.filter(|k| is_valid_storage_key(k)) else {
            return Err(PyValueError::new_err("invalid_key"));
        };
        let limit = self.effective_limit(max_bytes);
        let guard = self.lock()?;
        let Some(blob) = guard.items.get(&key) else {
            return Err(PyValueError::new_err("not_found"));
        };
        if blob.content.len() > limit {
            return Err(PyValueError::new_err("file_too_large"));
        }
        Ok(PyBytes::new(py, &blob.content))
    }

    fn metadata(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<StoredMeta> {
        let Some(key) = extract_utf8_opt(py, key)?.filter(|k| is_valid_storage_key(k)) else {
            return Err(PyValueError::new_err("invalid_key"));
        };
        let guard = self.lock()?;
        let Some(blob) = guard.items.get(&key) else {
            return Err(PyValueError::new_err("not_found"));
        };
        Ok((
            blob.key.clone(),
            blob.size,
            blob.content_type.clone(),
            blob.sha256.clone(),
            blob.filename.clone(),
        ))
    }

    fn exists(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        let Some(key) = extract_utf8_opt(py, key)?.filter(|k| is_valid_storage_key(k)) else {
            return Err(PyValueError::new_err("invalid_key"));
        };
        let guard = self.lock()?;
        Ok(guard.items.contains_key(&key))
    }

    fn delete(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        let Some(key) = extract_utf8_opt(py, key)?.filter(|k| is_valid_storage_key(k)) else {
            return Err(PyValueError::new_err("invalid_key"));
        };
        let mut guard = self.lock()?;
        let Some(removed) = guard.items.remove(&key) else {
            return Ok(false);
        };
        guard.total_size = guard.total_size.saturating_sub(removed.size);
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_and_media_validation() {
        assert!(is_valid_storage_key("ab/cd/abcdef012345"));
        assert!(!is_valid_storage_key("/leading"));
        assert!(!is_valid_storage_key("a/../b"));
        assert!(!is_valid_storage_key("Upper"));
        assert_eq!(normalize_content_type_str("Text/Plain"), "text/plain");
        assert_eq!(
            normalize_content_type_str("bad/type/extra"),
            "application/octet-stream"
        );
        assert_eq!(
            normalize_content_type_str("text/plain; charset=utf-8"),
            "application/octet-stream"
        );
        let k = make_storage_key();
        assert!(is_valid_storage_key(&k));
        assert_eq!(k.len(), 38);
    }
}
