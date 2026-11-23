//! Bounded, thread-safe in-process cache used by the Python Cache resource.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

struct Entry {
    value: Vec<u8>,
    expires_at: Instant,
    referenced: bool,
    generation: u64,
}

const MAX_TTL_SECONDS: f64 = 366.0 * 24.0 * 60.0 * 60.0;

#[derive(Default)]
struct State {
    entries: HashMap<String, Entry>,
    clock: VecDeque<(String, u64)>,
    bytes: usize,
    next_generation: u64,
}

/// Capacity-bounded cache with monotonic expiry and clock-style eviction.
#[pyclass(name = "MemoryCache", module = "hinoki.core")]
pub(crate) struct PyMemoryCache {
    max_entries: usize,
    max_bytes: usize,
    state: Mutex<State>,
}

impl PyMemoryCache {
    fn new(max_entries: usize, max_bytes: usize) -> Self {
        Self {
            max_entries,
            max_bytes,
            state: Mutex::new(State::default()),
        }
    }

    fn lock(&self) -> PyResult<std::sync::MutexGuard<'_, State>> {
        self.state
            .lock()
            .map_err(|_| PyRuntimeError::new_err("cache state lock is poisoned"))
    }

    fn evict_one(&self, state: &mut State, now: Instant) -> PyResult<()> {
        while let Some((key, generation)) = state.clock.pop_front() {
            let Some(entry) = state.entries.get_mut(&key) else {
                continue;
            };
            if entry.generation != generation {
                continue;
            }
            if entry.expires_at <= now {
                let removed = state.entries.remove(&key).expect("entry was checked");
                state.bytes -= removed.value.len();
                return Ok(());
            }
            if entry.referenced {
                entry.referenced = false;
                state.clock.push_back((key, generation));
                continue;
            }
            if let Some(removed) = state.entries.remove(&key) {
                state.bytes -= removed.value.len();
                return Ok(());
            }
        }
        Err(PyRuntimeError::new_err("cache clock is inconsistent"))
    }

    fn compact_clock(state: &mut State) {
        let threshold = state.entries.len().saturating_mul(2).saturating_add(16);
        if state.clock.len() <= threshold {
            return;
        }
        let entries = &state.entries;
        state.clock.retain(|(key, generation)| {
            entries
                .get(key)
                .is_some_and(|entry| entry.generation == *generation)
        });
    }
}

#[pymethods]
impl PyMemoryCache {
    #[new]
    fn py_new(max_entries: usize, max_bytes: usize) -> PyResult<Self> {
        if max_entries == 0 || max_bytes == 0 {
            return Err(PyValueError::new_err(
                "cache entry and byte limits must be positive",
            ));
        }
        Ok(Self::new(max_entries, max_bytes))
    }

    fn get(&self, key: &str) -> PyResult<Option<Vec<u8>>> {
        let mut state = self.lock()?;
        let now = Instant::now();
        let Some(entry) = state.entries.get(key) else {
            return Ok(None);
        };
        if entry.expires_at <= now {
            let expired = state.entries.remove(key);
            if let Some(item) = expired {
                state.bytes -= item.value.len();
            }
            return Ok(None);
        }
        let Some(entry) = state.entries.get_mut(key) else {
            return Ok(None);
        };
        entry.referenced = true;
        Ok(Some(entry.value.clone()))
    }

    fn set(&self, key: String, value: Vec<u8>, ttl_seconds: f64) -> PyResult<()> {
        if !ttl_seconds.is_finite() || ttl_seconds <= 0.0 || ttl_seconds > MAX_TTL_SECONDS {
            return Err(PyValueError::new_err(
                "cache TTL must be finite and positive",
            ));
        }
        if value.len() > self.max_bytes {
            return Err(PyValueError::new_err("cache value exceeds max_bytes"));
        }
        let ttl = Duration::try_from_secs_f64(ttl_seconds)
            .map_err(|_| PyValueError::new_err("cache TTL is out of range"))?;
        let mut state = self.lock()?;
        let now = Instant::now();
        let generation = state.next_generation;
        let next_generation = generation
            .checked_add(1)
            .ok_or_else(|| PyRuntimeError::new_err("cache generation limit reached"))?;
        if let Some(old) = state.entries.remove(&key) {
            state.bytes -= old.value.len();
        }
        while state.entries.len() >= self.max_entries || state.bytes > self.max_bytes - value.len()
        {
            self.evict_one(&mut state, now)?;
        }
        state.bytes += value.len();
        state.clock.push_back((key.clone(), generation));
        state.entries.insert(
            key,
            Entry {
                value,
                expires_at: now + ttl,
                referenced: false,
                generation,
            },
        );
        state.next_generation = next_generation;
        Self::compact_clock(&mut state);
        Ok(())
    }

    fn delete(&self, key: &str) -> PyResult<bool> {
        let mut state = self.lock()?;
        let removed = state.entries.remove(key);
        if let Some(entry) = removed {
            state.bytes -= entry.value.len();
            Ok(true)
        } else {
            Ok(false)
        }
    }

    fn clear(&self) -> PyResult<()> {
        *self.lock()? = State::default();
        Ok(())
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyMemoryCache>()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn evicts_to_both_limits_and_keeps_recently_referenced_entries() {
        let cache = PyMemoryCache::new(2, 5);
        cache.set("a".into(), b"aa".to_vec(), 60.0).unwrap();
        cache.set("b".into(), b"bb".to_vec(), 60.0).unwrap();
        assert_eq!(cache.get("a").unwrap(), Some(b"aa".to_vec()));
        cache.set("c".into(), b"cc".to_vec(), 60.0).unwrap();
        assert_eq!(cache.get("a").unwrap(), Some(b"aa".to_vec()));
        assert_eq!(cache.get("b").unwrap(), None);
        assert_eq!(cache.get("c").unwrap(), Some(b"cc".to_vec()));
    }

    #[test]
    fn rejects_invalid_ttls_and_oversized_values() {
        let cache = PyMemoryCache::new(2, 2);
        assert!(cache.set("a".into(), b"abc".to_vec(), 1.0).is_err());
        assert!(cache.set("a".into(), b"a".to_vec(), 0.0).is_err());
    }

    #[test]
    fn expires_entries_using_monotonic_time() {
        let cache = PyMemoryCache::new(2, 8);
        cache.set("a".into(), b"a".to_vec(), 0.001).unwrap();
        std::thread::sleep(Duration::from_millis(5));
        assert_eq!(cache.get("a").unwrap(), None);
    }

    #[test]
    fn serializes_concurrent_access_and_keeps_capacity_bounded() {
        let cache = std::sync::Arc::new(PyMemoryCache::new(16, 1024));
        let threads: Vec<_> = (0..8)
            .map(|worker| {
                let cache = std::sync::Arc::clone(&cache);
                std::thread::spawn(move || {
                    for index in 0..100 {
                        let key = format!("{worker}-{index}");
                        cache.set(key.clone(), b"v".to_vec(), 60.0).unwrap();
                        let _ = cache.get(&key).unwrap();
                    }
                })
            })
            .collect();
        for thread in threads {
            thread.join().unwrap();
        }
        let state = cache.lock().unwrap();
        assert!(state.entries.len() <= 16);
        assert!(state.bytes <= 1024);
    }
}
