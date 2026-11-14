//! PostgreSQL TLS URL options and rustls connector.

use std::io::Read;
use std::path::PathBuf;
use std::str::FromStr;

use deadpool_postgres::SslMode;
use percent_encoding::percent_decode_str;
use rustls::RootCertStore;
use rustls_pki_types::{pem::PemObject, CertificateDer};
use tokio_postgres_rustls::MakeRustlsConnect;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct TlsOptions {
    pub(crate) ssl_mode: SslMode,
    pub(crate) ssl_root_cert: Option<PathBuf>,
}

const MAX_ROOT_CERT_BYTES: usize = 1024 * 1024;

impl Default for TlsOptions {
    fn default() -> Self {
        Self {
            ssl_mode: SslMode::Prefer,
            ssl_root_cert: None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Parameter {
    key: String,
    value: String,
    start: usize,
    end: usize,
}

/// Remove Hinoki TLS parameters so tokio-postgres does not reject `sslrootcert`.
pub(crate) fn extract_options(input: &str) -> Result<(String, TlsOptions), ()> {
    let (remaining, mode, root) =
        if input.starts_with("postgres://") || input.starts_with("postgresql://") {
            extract_uri_options(input)?
        } else {
            extract_keyword_options(input)?
        };

    let mut options = TlsOptions::default();
    if let Some(mode) = mode {
        options.ssl_mode = parse_mode(&mode)?;
    }
    options.ssl_root_cert = root.map(PathBuf::from);
    if options.ssl_mode == SslMode::Disable && options.ssl_root_cert.is_some() {
        return Err(());
    }
    Ok((remaining, options))
}

fn extract_uri_options(input: &str) -> Result<(String, Option<String>, Option<String>), ()> {
    let Some(query_start) = input.find('?') else {
        return Ok((input.to_owned(), None, None));
    };
    let (base, query_and_suffix) = input.split_at(query_start);
    let query_and_suffix = &query_and_suffix[1..];
    let suffix_start = query_and_suffix.find('#').unwrap_or(query_and_suffix.len());
    let query = &query_and_suffix[..suffix_start];
    let suffix = &query_and_suffix[suffix_start..];
    let mut remaining = Vec::new();
    let mut mode = None;
    let mut root = None;

    for pair in query.split('&').filter(|pair| !pair.is_empty()) {
        let (raw_key, raw_value) = pair.split_once('=').unwrap_or((pair, ""));
        let key = percent_decode_str(raw_key).decode_utf8().map_err(|_| ())?;
        let value = percent_decode_str(raw_value)
            .decode_utf8()
            .map_err(|_| ())?;
        match key.as_ref() {
            "sslmode" => set_once(&mut mode, value.into_owned())?,
            "sslrootcert" => set_once(&mut root, value.into_owned())?,
            _ => remaining.push(pair),
        }
    }

    let mut output = base.to_owned();
    if !remaining.is_empty() {
        output.push('?');
        output.push_str(&remaining.join("&"));
    }
    output.push_str(suffix);
    Ok((output, mode, root))
}

fn extract_keyword_options(input: &str) -> Result<(String, Option<String>, Option<String>), ()> {
    let parameters = parse_parameters(input)?;
    let mut mode = None;
    let mut root = None;
    let mut removed = Vec::new();

    for parameter in parameters {
        match parameter.key.as_str() {
            "sslmode" => {
                set_once(&mut mode, parameter.value)?;
                removed.push((parameter.start, parameter.end));
            }
            "sslrootcert" => {
                set_once(&mut root, parameter.value)?;
                removed.push((parameter.start, parameter.end));
            }
            _ => {}
        }
    }

    let mut output = String::with_capacity(input.len());
    let mut previous = 0;
    for (start, end) in removed {
        output.push_str(&input[previous..start]);
        previous = end;
    }
    output.push_str(&input[previous..]);
    Ok((output, mode, root))
}

fn parse_parameters(input: &str) -> Result<Vec<Parameter>, ()> {
    let mut chars = input.char_indices().peekable();
    let mut parameters = Vec::new();

    loop {
        while matches!(chars.peek(), Some((_, ch)) if ch.is_whitespace()) {
            chars.next();
        }
        let Some(&(start, _)) = chars.peek() else {
            break;
        };
        let mut key = String::new();
        while let Some(&(_, ch)) = chars.peek() {
            if ch == '=' || ch.is_whitespace() {
                break;
            }
            key.push(ch);
            chars.next();
        }
        while matches!(chars.peek(), Some((_, ch)) if ch.is_whitespace()) {
            chars.next();
        }
        if key.is_empty() {
            return Err(());
        }
        let Some((equal_index, '=')) = chars.next() else {
            return Err(());
        };
        let mut end = equal_index + 1;
        while let Some((index, ch)) = chars.peek().copied() {
            if !ch.is_whitespace() {
                break;
            }
            chars.next();
            end = index + ch.len_utf8();
        }

        let mut value = String::new();
        if matches!(chars.peek(), Some((_, '\''))) {
            let (_, _) = chars.next().ok_or(())?;
            let mut closed = false;
            while let Some((index, ch)) = chars.next() {
                end = index + ch.len_utf8();
                if ch == '\\' {
                    let (escaped_index, escaped) = chars.next().ok_or(())?;
                    value.push(escaped);
                    end = escaped_index + escaped.len_utf8();
                } else if ch == '\'' {
                    closed = true;
                    break;
                } else {
                    value.push(ch);
                }
            }
            if !closed || matches!(chars.peek(), Some((_, ch)) if !ch.is_whitespace()) {
                return Err(());
            }
        } else {
            while let Some((index, ch)) = chars.peek().copied() {
                if ch.is_whitespace() {
                    break;
                }
                chars.next();
                end = index + ch.len_utf8();
                if ch == '\\' {
                    let (escaped_index, escaped) = chars.next().ok_or(())?;
                    value.push(escaped);
                    end = escaped_index + escaped.len_utf8();
                } else {
                    value.push(ch);
                }
            }
        }
        parameters.push(Parameter {
            key,
            value,
            start,
            end,
        });
    }

    Ok(parameters)
}

fn set_once(target: &mut Option<String>, value: String) -> Result<(), ()> {
    if target.replace(value).is_some() {
        return Err(());
    }
    Ok(())
}

fn parse_mode(value: &str) -> Result<SslMode, ()> {
    match value {
        "disable" => Ok(SslMode::Disable),
        "prefer" => Ok(SslMode::Prefer),
        "require" | "verify-ca" | "verify-full" => Ok(SslMode::Require),
        _ => Err(()),
    }
}

pub(crate) fn connector(options: &TlsOptions) -> Result<MakeRustlsConnect, ()> {
    let mut roots = RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());

    if let Some(path) = &options.ssl_root_cert {
        let file = std::fs::File::open(path).map_err(|_| ())?;
        let mut pem = Vec::with_capacity(MAX_ROOT_CERT_BYTES);
        file.take((MAX_ROOT_CERT_BYTES + 1) as u64)
            .read_to_end(&mut pem)
            .map_err(|_| ())?;
        if pem.len() > MAX_ROOT_CERT_BYTES {
            return Err(());
        }
        let mut count = 0;
        for certificate in CertificateDer::pem_slice_iter(&pem) {
            roots.add(certificate.map_err(|_| ())?).map_err(|_| ())?;
            count += 1;
        }
        if count == 0 {
            return Err(());
        }
    }

    let config = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    Ok(MakeRustlsConnect::new(config))
}

impl FromStr for TlsOptions {
    type Err = ();

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        extract_options(s).map(|(_, options)| options)
    }
}

#[cfg(test)]
mod tests {
    use super::{extract_options, TlsOptions};
    use deadpool_postgres::SslMode;
    use std::path::PathBuf;
    use std::str::FromStr;

    #[test]
    fn uri_tls_options_are_removed_and_decoded() {
        let (uri, options) = extract_options(
            "postgresql://u:p@db/app?application_name=site&sslmode=require&sslrootcert=%2Frun%2Fdb%20ca.pem",
        )
        .unwrap();
        assert_eq!(uri, "postgresql://u:p@db/app?application_name=site");
        assert_eq!(options.ssl_mode, SslMode::Require);
        assert_eq!(options.ssl_root_cert, Some(PathBuf::from("/run/db ca.pem")));

        for (encoded, decoded) in [
            ("/run/team+db.pem", "/run/team+db.pem"),
            ("/run/team%2Bdb.pem", "/run/team+db.pem"),
            ("/run/team%20db.pem", "/run/team db.pem"),
        ] {
            let uri = format!("postgres://db/app?sslrootcert={encoded}");
            let (_, options) = extract_options(&uri).unwrap();
            assert_eq!(options.ssl_root_cert, Some(PathBuf::from(decoded)));
        }
        assert!(extract_options("postgres://db/app?sslrootcert=%FF").is_err());

        let (_, verify_full) = extract_options("postgres://db/app?sslmode=verify-full").unwrap();
        assert_eq!(verify_full.ssl_mode, SslMode::Require);
    }

    #[test]
    fn keyword_tls_options_are_removed_without_rewriting_other_values() {
        let (conninfo, options) = extract_options(
            "host = db password = 'a b' sslmode = require sslrootcert = '/run/db ca.pem' dbname = app",
        )
        .unwrap();
        assert!(tokio_postgres::Config::from_str(&conninfo).is_ok());
        assert_eq!(options.ssl_mode, SslMode::Require);
        assert_eq!(options.ssl_root_cert, Some(PathBuf::from("/run/db ca.pem")));
    }

    #[test]
    fn unsupported_duplicate_and_inconsistent_tls_options_fail_closed() {
        for invalid in [
            "postgres://db/app?sslmode=require&sslmode=disable",
            "postgres://db/app?sslmode=disable&sslrootcert=%2Ftmp%2Fca.pem",
            "host=db sslmode=allow",
        ] {
            assert!(TlsOptions::from_str(invalid).is_err(), "{invalid}");
        }
    }
}
