//! TLS for the listener: certificate and key loading, and the acceptor.
//!
//! rustls only (no OpenSSL), the `ring` provider passed explicitly so nothing
//! depends on a process-wide default, TLS 1.2 and 1.3 with rustls' safe default
//! cipher suites, no client authentication, ALPN `http/1.1`, plus `h2` first when
//! HTTP/2 is on. The files are read once, when the server starts; the
//! private key is never logged and errors name the file, never its content.

use std::fmt;
use std::path::Path;
use std::sync::Arc;

use rustls::crypto::ring::default_provider;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use rustls::ServerConfig;
pub use tokio_rustls::TlsAcceptor;

/// Why a certificate or key could not be used.
#[derive(Debug)]
pub struct TlsError(String);

impl fmt::Display for TlsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for TlsError {}

fn failure(what: &str, path: &Path, reason: impl fmt::Display) -> TlsError {
    TlsError(format!("{what} {}: {reason}", path.display()))
}

/// An acceptor for the PEM certificate chain in `cert` and the PEM private key
/// (PKCS#8, PKCS#1 or SEC1) in `key`. With `http2` the ALPN offer is `h2` then
/// `http/1.1`; without it, `http/1.1` only.
pub fn acceptor(cert: &Path, key: &Path, http2: bool) -> Result<TlsAcceptor, TlsError> {
    let chain = CertificateDer::pem_file_iter(cert)
        .map_err(|e| failure("cannot read the certificate", cert, e))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| failure("invalid certificate", cert, e))?;
    if chain.is_empty() {
        return Err(failure("no certificate in", cert, "expected a PEM file"));
    }
    let private =
        PrivateKeyDer::from_pem_file(key).map_err(|e| failure("cannot use the key", key, e))?;
    let mut config = ServerConfig::builder_with_provider(Arc::new(default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|e| TlsError(format!("TLS versions: {e}")))?
        .with_no_client_auth()
        .with_single_cert(chain, private)
        .map_err(|e| {
            TlsError(format!(
                "certificate and key do not match or are unusable: {e}"
            ))
        })?;
    config.alpn_protocols = if http2 {
        vec![b"h2".to_vec(), b"http/1.1".to_vec()]
    } else {
        vec![b"http/1.1".to_vec()]
    };
    Ok(TlsAcceptor::from(Arc::new(config)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(name: &str) -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/tls")
            .join(name)
    }

    #[test]
    fn the_fixture_pair_loads() {
        acceptor(
            &fixture("localhost-cert.pem"),
            &fixture("localhost-key.pem"),
            false,
        )
        .unwrap();
    }

    #[test]
    fn a_missing_file_names_it() {
        let err = acceptor(&fixture("absent.pem"), &fixture("localhost-key.pem"), false)
            .err()
            .unwrap();
        assert!(err.to_string().contains("absent.pem"), "{err}");
    }

    #[test]
    fn a_key_in_the_certificate_slot_is_refused() {
        let key = fixture("localhost-key.pem");
        assert!(acceptor(&key, &key, false).is_err());
    }

    #[test]
    fn a_certificate_in_the_key_slot_is_refused() {
        let cert = fixture("localhost-cert.pem");
        let err = acceptor(&cert, &cert, false).err().unwrap();
        assert!(!err.to_string().contains("BEGIN"), "{err}");
    }
}
