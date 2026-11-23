//! The rustls client configuration: bundled public roots plus an optional PEM
//! file. Certificate and host name verification cannot be turned off.

use std::io::Read;
use std::path::Path;
use std::sync::Arc;

use rustls::crypto::ring::default_provider;
use rustls::{ClientConfig, RootCertStore};
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::CertificateDer;
use tokio_rustls::TlsConnector;

use crate::error::ConfigError;

const MAX_CA_BYTES: usize = 1024 * 1024;

fn read_ca(path: &Path) -> Result<Vec<u8>, ConfigError> {
    let unreadable = ConfigError("tls_ca_file cannot be read");
    let file = std::fs::File::open(path).map_err(|_| unreadable.clone())?;
    let mut pem = Vec::new();
    file.take(MAX_CA_BYTES as u64 + 1)
        .read_to_end(&mut pem)
        .map_err(|_| unreadable)?;
    if pem.len() > MAX_CA_BYTES {
        return Err(ConfigError("tls_ca_file is larger than 1 MiB"));
    }
    Ok(pem)
}

fn add_ca(roots: &mut RootCertStore, pem: &[u8]) -> Result<(), ConfigError> {
    let invalid = ConfigError("tls_ca_file has no valid certificate");
    let mut count = 0;
    for certificate in CertificateDer::pem_slice_iter(pem) {
        let certificate = certificate.map_err(|_| invalid.clone())?;
        roots.add(certificate).map_err(|_| invalid.clone())?;
        count += 1;
    }
    if count == 0 {
        return Err(invalid);
    }
    Ok(())
}

/// A connector that trusts the bundled roots and the certificates in `ca_file`.
pub(crate) fn connector(ca_file: Option<&Path>) -> Result<TlsConnector, ConfigError> {
    let mut roots = RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    if let Some(path) = ca_file {
        add_ca(&mut roots, &read_ca(path)?)?;
    }
    let config = ClientConfig::builder_with_provider(Arc::new(default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|_| ConfigError("TLS is not available"))?
        .with_root_certificates(roots)
        .with_no_client_auth();
    Ok(TlsConnector::from(Arc::new(config)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn fixture(name: &str) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/tls")
            .join(name)
    }

    #[test]
    fn the_bundled_roots_alone_are_enough() {
        assert!(connector(None).is_ok());
    }

    #[test]
    fn a_ca_file_is_added() {
        assert!(connector(Some(&fixture("localhost-cert.pem"))).is_ok());
    }

    #[test]
    fn unusable_ca_files_are_refused() {
        let missing = connector(Some(&fixture("missing.pem"))).err().unwrap();
        assert_eq!(missing, ConfigError("tls_ca_file cannot be read"));
        // A private key is a PEM file without any certificate.
        let no_certificate = connector(Some(&fixture("localhost-key.pem")))
            .err()
            .unwrap();
        assert_eq!(
            no_certificate,
            ConfigError("tls_ca_file has no valid certificate")
        );
        let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml");
        assert!(connector(Some(&manifest)).is_err());
    }
}
