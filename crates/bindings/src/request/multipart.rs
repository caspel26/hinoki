use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use super::parse::negotiate;

pyo3::create_exception!(core, MultipartLimitError, PyValueError);

/// One part of a `multipart/form-data` body.
#[derive(Debug, PartialEq)]
pub struct Part {
    pub name: String,
    pub filename: Option<String>,
    pub content_type: String,
    pub data: Vec<u8>,
}

const MAX_HEADER_BLOCK: usize = 8192;
const MAX_BOUNDARY: usize = 70;

fn find(hay: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if from > hay.len() || needle.is_empty() {
        return None;
    }
    hay[from..]
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|at| at + from)
}

/// `;`-separated parameters of a header value, honouring quoted strings with
/// backslash escapes. Names are lower-cased.
fn header_params(value: &str) -> Vec<(String, String)> {
    let mut params = Vec::new();
    let mut current = String::new();
    let mut quoted = false;
    let mut escaped = false;
    let mut pieces = Vec::new();
    for ch in value.chars() {
        if escaped {
            current.push(ch);
            escaped = false;
        } else if quoted && ch == '\\' {
            escaped = true;
        } else if ch == '"' {
            quoted = !quoted;
            current.push(ch);
        } else if ch == ';' && !quoted {
            pieces.push(std::mem::take(&mut current));
        } else {
            current.push(ch);
        }
    }
    pieces.push(current);
    for piece in pieces.iter().skip(1) {
        if let Some((name, raw)) = piece.split_once('=') {
            let raw = raw.trim();
            let raw = raw
                .strip_prefix('"')
                .and_then(|v| v.strip_suffix('"'))
                .unwrap_or(raw);
            params.push((name.trim().to_lowercase(), raw.to_owned()));
        }
    }
    params
}

fn part_headers(block: &[u8]) -> Result<(String, Option<String>, String), &'static str> {
    let text = std::str::from_utf8(block).map_err(|_| "part headers are not UTF-8")?;
    let (mut name, mut filename, mut content_type) = (None, None, String::from("text/plain"));
    let mut content_type_seen = false;
    for line in text.split("\r\n") {
        check_header_line(line)?;
        let Some((key, value)) = line.split_once(':') else {
            return Err("malformed part header");
        };
        match key.trim().to_lowercase().as_str() {
            "content-disposition" => parse_content_disposition(value, &mut name, &mut filename)?,
            "content-type" => {
                if content_type_seen {
                    return Err("duplicate Content-Type part header");
                }
                content_type_seen = true;
                content_type = value.trim().to_owned();
            }
            _ => {}
        }
    }
    Ok((name.ok_or("part has no name")?, filename, content_type))
}

fn check_header_line(line: &str) -> Result<(), &'static str> {
    if line.chars().any(|c| c.is_control() && c != '\t') {
        return Err("control character in a part header");
    }
    Ok(())
}

fn parse_content_disposition(
    value: &str,
    name: &mut Option<String>,
    filename: &mut Option<String>,
) -> Result<(), &'static str> {
    if !value
        .split(';')
        .next()
        .is_some_and(|v| v.trim().eq_ignore_ascii_case("form-data"))
    {
        return Err("part is not form-data");
    }
    for (param, found) in header_params(value) {
        let slot = match param.as_str() {
            "name" => &mut *name,
            "filename" => &mut *filename,
            _ => continue,
        };
        if slot.replace(found).is_some() {
            return Err("duplicate parameter in a part header");
        }
    }
    Ok(())
}

/// Split a `multipart/form-data` body (RFC 7578). Bounded: at most `max_parts`
/// parts and `max_part_bytes` per part; the whole body is already capped by the
/// caller. A file name is returned as sent and must never be used as a path.
pub fn parse_multipart(
    body: &[u8],
    boundary: &str,
    max_parts: usize,
    max_part_bytes: usize,
) -> Result<Vec<Part>, MultipartError> {
    if boundary.is_empty() || boundary.len() > MAX_BOUNDARY || boundary.bytes().any(|b| b < 0x20) {
        return Err(MultipartError::Invalid("invalid multipart boundary"));
    }
    let opening = [b"--".as_slice(), boundary.as_bytes()].concat();
    let delimiter = [b"\r\n".as_slice(), &opening].concat();
    if !body.starts_with(&opening) {
        return Err(MultipartError::Invalid(
            "multipart body does not start with the boundary",
        ));
    }
    let mut at = opening.len();
    let mut parts = Vec::new();
    loop {
        if body[at..].starts_with(b"--") {
            return Ok(parts);
        }
        if !body[at..].starts_with(b"\r\n") {
            return Err(MultipartError::Invalid("malformed multipart delimiter"));
        }
        at += 2;
        if parts.len() >= max_parts {
            return Err(MultipartError::Invalid("too many multipart parts"));
        }
        let end = find(body, b"\r\n\r\n", at)
            .ok_or(MultipartError::Invalid("unterminated part headers"))?;
        if end - at > MAX_HEADER_BLOCK {
            return Err(MultipartError::Invalid("part headers are too large"));
        }
        let (name, filename, content_type) =
            part_headers(&body[at..end]).map_err(MultipartError::Invalid)?;
        at = end + 4;
        let stop = find(body, &delimiter, at)
            .ok_or(MultipartError::Invalid("multipart body is not terminated"))?;
        if stop - at > max_part_bytes {
            return Err(MultipartError::LimitExceeded);
        }
        parts.push(Part {
            name,
            filename,
            content_type,
            data: body[at..stop].to_vec(),
        });
        at = stop + delimiter.len();
    }
}

#[derive(Debug, PartialEq)]
pub enum MultipartError {
    Invalid(&'static str),
    LimitExceeded,
}

/// Split a multipart body. Returns `(fields, files)`: fields are
/// `(name, text)`, files are `(name, filename, content_type, bytes)`. Raises
/// `ValueError` for malformed or over-limit bodies and non-UTF-8 text fields.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn parse_multipart_body<'py>(
    py: Python<'py>,
    raw: &[u8],
    boundary: &str,
    max_parts: usize,
    max_part_bytes: usize,
) -> PyResult<(
    Vec<(String, String)>,
    Vec<(String, String, String, Bound<'py, PyBytes>)>,
)> {
    let parts =
        parse_multipart(raw, boundary, max_parts, max_part_bytes).map_err(|error| match error {
            MultipartError::Invalid(message) => PyValueError::new_err(message),
            MultipartError::LimitExceeded => {
                MultipartLimitError::new_err("multipart part is too large")
            }
        })?;
    let (mut fields, mut files) = (Vec::new(), Vec::new());
    for part in parts {
        match part.filename {
            Some(filename) => files.push((
                part.name,
                filename,
                part.content_type,
                PyBytes::new(py, &part.data),
            )),
            None => {
                let text = String::from_utf8(part.data)
                    .map_err(|_| PyValueError::new_err("a form field is not valid UTF-8"))?;
                fields.push((part.name, text));
            }
        }
    }
    Ok((fields, files))
}

/// Index into `available` of the best match for an `Accept` header, or `None`.
#[pyfunction]
pub fn negotiate_media(accept: &str, available: Vec<String>) -> Option<usize> {
    negotiate(accept, &available)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn multipart_splits_fields_and_files() {
        let body = b"--XX\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nHello\r\n--XX\r\nContent-Disposition: form-data; name=\"doc\"; filename=\"a \\\"b\\\".txt\"\r\nContent-Type: text/csv\r\n\r\n1,2 a--XX b\r\n--XX--\r\nepilogue";
        let parts = parse_multipart(body, "XX", 10, 100).unwrap();
        assert_eq!(parts.len(), 2);
        assert_eq!(
            (parts[0].name.as_str(), parts[0].data.as_slice()),
            ("title", &b"Hello"[..])
        );
        assert_eq!(parts[0].filename, None);
        assert_eq!(parts[1].filename.as_deref(), Some("a \"b\".txt"));
        assert_eq!(parts[1].content_type, "text/csv");
        assert_eq!(parts[1].data, b"1,2 a--XX b");
    }

    #[test]
    fn multipart_rejects_malformed_and_oversized_bodies() {
        let ok = b"--B\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nxyz\r\n--B--";
        assert!(parse_multipart(ok, "B", 1, 3).is_ok());
        assert_eq!(
            parse_multipart(ok, "B", 1, 2),
            Err(MultipartError::LimitExceeded)
        );
        assert!(parse_multipart(ok, "B", 0, 3).is_err());
        assert!(parse_multipart(b"junk", "B", 1, 3).is_err());
        assert!(parse_multipart(
            b"--B\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nxyz",
            "B",
            1,
            9
        )
        .is_err());
        assert!(parse_multipart(
            b"--B\r\nContent-Disposition: attachment; name=\"a\"\r\n\r\nx\r\n--B--",
            "B",
            1,
            9
        )
        .is_err());
        assert!(parse_multipart(
            b"--B\r\nContent-Disposition: form-data\r\n\r\nx\r\n--B--",
            "B",
            1,
            9
        )
        .is_err());
        assert!(parse_multipart(ok, "", 1, 3).is_err());
        assert!(parse_multipart(ok, &"b".repeat(71), 1, 3).is_err());
    }

    #[test]
    fn multipart_rejects_repeated_part_content_type_headers() {
        for headers in [
            "Content-Type: text/plain\r\nContent-Type: text/plain",
            "Content-Type: text/plain\r\ncontent-type: application/octet-stream",
        ] {
            let body = format!(
                "--B\r\nContent-Disposition: form-data; name=\"f\"; filename=\"x\"\r\n{headers}\r\n\r\nx\r\n--B--"
            );
            assert_eq!(
                parse_multipart(body.as_bytes(), "B", 1, 1),
                Err(MultipartError::Invalid(
                    "duplicate Content-Type part header"
                ))
            );
        }
    }
}
