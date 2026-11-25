//! Thread-safe, bounded in-memory session store with revocation tombstones.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

struct StoredSession {
    data_json: Vec<u8>,
    created_us: i64,
    expires_us: i64,
    idle_expires_us: i64,
    last_seen_us: i64,
    subject: Option<String>,
}

type SessionTuple<'py> = (Bound<'py, PyBytes>, i64, i64, i64, i64, Option<String>);

#[derive(Default)]
struct State {
    records: HashMap<String, StoredSession>,
    /// FIFO list of `(token_hash, absolute_expires_us)` tombstones.
    revoked_order: VecDeque<String>,
    revoked: HashMap<String, i64>,
}

impl State {
    fn revoke(&mut self, token_hash: &str, max_sessions: usize) {
        let Some(record) = self.records.remove(token_hash) else {
            return;
        };
        if self
            .revoked
            .insert(token_hash.to_owned(), record.expires_us)
            .is_some()
        {
            self.revoked_order.retain(|k| k != token_hash);
        }
        self.revoked_order.push_back(token_hash.to_owned());
        while self.revoked.len() > max_sessions {
            if let Some(oldest) = self.revoked_order.pop_front() {
                self.revoked.remove(&oldest);
            } else {
                break;
            }
        }
    }

    fn prune(&mut self, now_us: i64, max_sessions: usize) -> usize {
        let expired: Vec<String> = self
            .records
            .iter()
            .filter(|(_, v)| v.expires_us <= now_us)
            .map(|(k, _)| k.clone())
            .collect();
        for key in &expired {
            self.records.remove(key);
        }
        let idle: Vec<String> = self
            .records
            .iter()
            .filter(|(_, v)| v.idle_expires_us <= now_us)
            .map(|(k, _)| k.clone())
            .collect();
        for key in &idle {
            self.revoke(key, max_sessions);
        }
        let stale: Vec<String> = self
            .revoked
            .iter()
            .filter(|(_, &until)| until <= now_us)
            .map(|(k, _)| k.clone())
            .collect();
        if !stale.is_empty() {
            for key in &stale {
                self.revoked.remove(key);
            }
            self.revoked_order.retain(|k| self.revoked.contains_key(k));
        }
        expired.len()
    }
}

#[pyclass(name = "NativeMemorySessionStore", module = "hinoki.core")]
pub struct NativeMemorySessionStore {
    max_sessions: usize,
    state: Mutex<State>,
}

impl NativeMemorySessionStore {
    fn lock(&self) -> PyResult<std::sync::MutexGuard<'_, State>> {
        self.state
            .lock()
            .map_err(|_| PyRuntimeError::new_err("session store lock is poisoned"))
    }
}

#[pymethods]
impl NativeMemorySessionStore {
    #[new]
    fn new(max_sessions: usize) -> PyResult<Self> {
        if max_sessions == 0 {
            return Err(PyValueError::new_err("max_sessions must be positive"));
        }
        Ok(Self {
            max_sessions,
            state: Mutex::new(State::default()),
        })
    }

    fn load<'py>(&self, py: Python<'py>, token_hash: &str) -> PyResult<Option<SessionTuple<'py>>> {
        let guard = self.lock()?;
        let Some(rec) = guard.records.get(token_hash) else {
            return Ok(None);
        };
        Ok(Some((
            PyBytes::new(py, &rec.data_json),
            rec.created_us,
            rec.expires_us,
            rec.idle_expires_us,
            rec.last_seen_us,
            rec.subject.clone(),
        )))
    }

    #[pyo3(signature = (token_hash, data_json, created_us, expires_us, idle_expires_us, last_seen_us, subject, now_us))]
    #[allow(clippy::too_many_arguments)]
    fn save(
        &self,
        token_hash: String,
        data_json: &[u8],
        created_us: i64,
        expires_us: i64,
        idle_expires_us: i64,
        last_seen_us: i64,
        subject: Option<String>,
        now_us: i64,
    ) -> PyResult<()> {
        let mut guard = self.lock()?;
        guard.prune(now_us, self.max_sessions);
        if guard.revoked.contains_key(&token_hash) {
            return Err(PyValueError::new_err("revoked"));
        }
        if !guard.records.contains_key(&token_hash) && guard.records.len() >= self.max_sessions {
            return Err(PyValueError::new_err("capacity"));
        }
        guard.records.insert(
            token_hash,
            StoredSession {
                data_json: data_json.to_vec(),
                created_us,
                expires_us,
                idle_expires_us,
                last_seen_us,
                subject,
            },
        );
        Ok(())
    }

    fn delete(&self, token_hash: &str) -> PyResult<()> {
        let mut guard = self.lock()?;
        guard.revoke(token_hash, self.max_sessions);
        Ok(())
    }

    fn delete_by_subject(&self, subject: &str) -> PyResult<usize> {
        let mut guard = self.lock()?;
        let keys: Vec<String> = guard
            .records
            .iter()
            .filter(|(_, v)| v.subject.as_deref() == Some(subject))
            .map(|(k, _)| k.clone())
            .collect();
        for key in &keys {
            guard.revoke(key, self.max_sessions);
        }
        Ok(keys.len())
    }

    fn purge_expired(&self, now_us: i64) -> PyResult<usize> {
        let mut guard = self.lock()?;
        Ok(guard.prune(now_us, self.max_sessions))
    }

    fn record_keys(&self) -> PyResult<Vec<String>> {
        let guard = self.lock()?;
        Ok(guard.records.keys().cloned().collect())
    }

    fn revoked_keys(&self) -> PyResult<Vec<String>> {
        let guard = self.lock()?;
        Ok(guard.revoked_order.iter().cloned().collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn revokes_prunes_and_bounds_tombstones() {
        let mut st = State::default();
        for (k, exp, idle) in [("a", 100, 50), ("b", 100, 50), ("c", 100, 50)] {
            st.records.insert(
                k.to_owned(),
                StoredSession {
                    data_json: b"{}".to_vec(),
                    created_us: 0,
                    expires_us: exp,
                    idle_expires_us: idle,
                    last_seen_us: 0,
                    subject: Some("u1".to_owned()),
                },
            );
        }
        st.revoke("a", 2);
        st.revoke("b", 2);
        st.revoke("c", 2);
        assert_eq!(
            st.revoked_order.iter().cloned().collect::<Vec<_>>(),
            vec!["b".to_owned(), "c".to_owned()]
        );
        assert_eq!(st.prune(101, 2), 0);
        assert!(st.revoked.is_empty());
    }
}
