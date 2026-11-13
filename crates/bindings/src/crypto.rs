//! Password hashing (Argon2id, PHC strings) and random tokens.
//!
//! The pure functions carry the unit tests; the `#[pyfunction]`s release the GIL
//! while hashing so a worker thread can do the work off the event loop.
//! Passwords, hashes and tokens never appear in an error message.

use argon2::{Algorithm, Argon2, Params, PasswordHash, PasswordHasher, PasswordVerifier, Version};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// Longest password accepted (bytes): bounds the work an attacker can ask for.
pub const MAX_PASSWORD: usize = 4096;
/// Upper bounds on parameters read from a stored hash (a corrupted or hostile
/// value must not allocate gigabytes or run for minutes).
const MAX_MEMORY_KIB: u32 = 1024 * 1024;
const MAX_TIME_COST: u32 = 64;
const MAX_LANES: u32 = 16;
const MAX_RANDOM: usize = 1024;

fn argon(memory: u32, time: u32, lanes: u32) -> Result<Argon2<'static>, &'static str> {
    if memory > MAX_MEMORY_KIB || time > MAX_TIME_COST || lanes > MAX_LANES {
        return Err("argon2 parameters are out of range");
    }
    let params = Params::new(memory, time, lanes, None).map_err(|_| "invalid argon2 parameters")?;
    Ok(Argon2::new(Algorithm::Argon2id, Version::V0x13, params))
}

/// Argon2id hash of `password` with a fresh random salt, as a PHC string.
pub fn hash(password: &[u8], memory: u32, time: u32, lanes: u32) -> Result<String, &'static str> {
    if password.len() > MAX_PASSWORD {
        return Err("password is too long");
    }
    let hasher = argon(memory, time, lanes)?;
    let hashed = hasher
        .hash_password(password)
        .map_err(|_| "hashing failed")?;
    Ok(hashed.to_string())
}

/// `Ok(true)` for a match, `Ok(false)` for a wrong password, `Err` when the
/// stored hash is not a usable Argon2 PHC string. The comparison is constant
/// time (inside the `argon2` crate).
pub fn verify(password: &[u8], stored: &str) -> Result<bool, &'static str> {
    if password.len() > MAX_PASSWORD {
        return Ok(false);
    }
    let parsed = PasswordHash::new(stored).map_err(|_| "malformed password hash")?;
    let params = Params::try_from(&parsed).map_err(|_| "malformed password hash")?;
    // Hash parameters come from storage: check them before doing any work.
    let hasher = argon(params.m_cost(), params.t_cost(), params.p_cost())?;
    Ok(hasher.verify_password(password, &parsed).is_ok())
}

/// True when `stored` was made with weaker or different parameters than the
/// ones given (or is not Argon2id): the caller should rehash after a login.
pub fn needs_rehash(
    stored: &str,
    memory: u32,
    time: u32,
    lanes: u32,
) -> Result<bool, &'static str> {
    let parsed = PasswordHash::new(stored).map_err(|_| "malformed password hash")?;
    let params = Params::try_from(&parsed).map_err(|_| "malformed password hash")?;
    let id = parsed.algorithm.as_str();
    Ok(id != "argon2id"
        || params.m_cost() != memory
        || params.t_cost() != time
        || params.p_cost() != lanes)
}

/// `count` bytes from the operating system's CSPRNG, URL-safe base64 without padding.
pub fn random_token(count: usize) -> Result<String, &'static str> {
    if count == 0 || count > MAX_RANDOM {
        return Err("token size must be between 1 and 1024 bytes");
    }
    let mut bytes = vec![0_u8; count];
    getrandom::fill(&mut bytes).map_err(|_| "the system random generator failed")?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

/// Constant-time equality of two byte strings (length differences are not hidden).
pub fn equal(a: &[u8], b: &[u8]) -> bool {
    use subtle::ConstantTimeEq;
    a.ct_eq(b).into()
}

#[pyfunction]
pub fn hash_password(
    py: Python<'_>,
    password: &str,
    memory_kib: u32,
    time_cost: u32,
    lanes: u32,
) -> PyResult<String> {
    let bytes = password.as_bytes().to_vec();
    py.detach(move || hash(&bytes, memory_kib, time_cost, lanes))
        .map_err(PyValueError::new_err)
}

#[pyfunction]
pub fn verify_password(py: Python<'_>, password: &str, stored: &str) -> PyResult<bool> {
    let bytes = password.as_bytes().to_vec();
    let stored = stored.to_owned();
    py.detach(move || verify(&bytes, &stored))
        .map_err(PyValueError::new_err)
}

#[pyfunction]
pub fn password_needs_rehash(
    stored: &str,
    memory_kib: u32,
    time_cost: u32,
    lanes: u32,
) -> PyResult<bool> {
    needs_rehash(stored, memory_kib, time_cost, lanes).map_err(PyValueError::new_err)
}

#[pyfunction]
pub fn make_token(count: usize) -> PyResult<String> {
    random_token(count).map_err(PyValueError::new_err)
}

#[pyfunction]
pub fn constant_time_equal(a: &[u8], b: &[u8]) -> bool {
    equal(a, b)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Small parameters keep the tests fast; they are valid Argon2id inputs.
    const M: u32 = 64;
    const T: u32 = 1;
    const P: u32 = 1;

    #[test]
    fn a_hash_verifies_only_its_own_password() {
        let stored = hash(b"correct horse", M, T, P).unwrap();
        assert!(stored.starts_with("$argon2id$v=19$m=64,t=1,p=1$"));
        assert_eq!(verify(b"correct horse", &stored), Ok(true));
        assert_eq!(verify(b"wrong", &stored), Ok(false));
        assert_eq!(verify(b"", &stored), Ok(false));
    }

    #[test]
    fn salts_are_random() {
        assert_ne!(hash(b"x", M, T, P).unwrap(), hash(b"x", M, T, P).unwrap());
    }

    #[test]
    fn malformed_stored_hashes_are_errors_not_mismatches() {
        for bad in ["", "plaintext", "$argon2id$v=19$m=64$x", "$2b$12$abc"] {
            assert!(verify(b"x", bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn hostile_parameters_are_refused_before_any_work() {
        let huge = "$argon2id$v=19$m=4194304,t=1,p=1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g";
        assert!(verify(b"x", huge).is_err());
        assert!(hash(b"x", MAX_MEMORY_KIB + 1, 1, 1).is_err());
        assert!(hash(b"x", 64, MAX_TIME_COST + 1, 1).is_err());
        assert!(hash(&vec![b'a'; MAX_PASSWORD + 1], M, T, P).is_err());
        assert_eq!(
            verify(&vec![b'a'; MAX_PASSWORD + 1], &hash(b"x", M, T, P).unwrap()),
            Ok(false)
        );
    }

    #[test]
    fn rehash_is_needed_when_parameters_change() {
        let stored = hash(b"x", M, T, P).unwrap();
        assert_eq!(needs_rehash(&stored, M, T, P), Ok(false));
        assert_eq!(needs_rehash(&stored, M * 2, T, P), Ok(true));
        assert_eq!(needs_rehash(&stored, M, T + 1, P), Ok(true));
        assert!(needs_rehash("nope", M, T, P).is_err());
    }

    #[test]
    fn tokens_are_url_safe_and_bounded() {
        let token = random_token(32).unwrap();
        assert_eq!(token.len(), 43);
        assert!(token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'));
        assert_ne!(token, random_token(32).unwrap());
        assert!(random_token(0).is_err());
        assert!(random_token(MAX_RANDOM + 1).is_err());
    }

    #[test]
    fn equality_is_exact() {
        assert!(equal(b"abc", b"abc"));
        assert!(!equal(b"abc", b"abd"));
        assert!(!equal(b"abc", b"ab"));
    }
}
