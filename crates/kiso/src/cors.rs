//! The CORS headers every answer carries once a CORS policy is configured.
//! Mirrors `CorsPolicy.headers` in `hinoki.runtime.origins`; a Python test pins
//! the two together.

use crate::headers::Header;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PreflightError {
    OriginDenied,
}
#[derive(Debug, Clone, Default)]
pub struct Cors {
    pub any_origin: bool,
    /// Allowed origins, lower case.
    pub origins: Vec<Vec<u8>>,
    pub credentials: bool,
    pub expose: Vec<u8>,
    /// Headers owned by an allowed preflight response.
    pub preflight: Vec<Header>,
}

impl Cors {
    /// Headers for a request whose `Origin` header was `origin` (empty when absent).
    pub fn headers(&self, origin: &[u8]) -> Vec<Header> {
        let mut head: Vec<Header> = if self.any_origin {
            if origin.is_empty() {
                Vec::new()
            } else {
                vec![(b"access-control-allow-origin".to_vec(), b"*".to_vec())]
            }
        } else if self.origins.iter().any(|o| o == origin) {
            vec![
                (b"access-control-allow-origin".to_vec(), origin.to_vec()),
                (b"vary".to_vec(), b"origin".to_vec()),
            ]
        } else {
            return vec![(b"vary".to_vec(), b"origin".to_vec())];
        };
        if !head.is_empty() && self.credentials {
            head.push((
                b"access-control-allow-credentials".to_vec(),
                b"true".to_vec(),
            ));
        }
        if !head.is_empty() && !self.expose.is_empty() {
            head.push((
                b"access-control-expose-headers".to_vec(),
                self.expose.clone(),
            ));
        }
        head
    }

    /// The preflight answer's own headers, or the denied-origin condition.
    pub fn preflight(
        &self,
        method: &str,
        origin: Option<&[u8]>,
        requests_method: bool,
    ) -> Result<Option<Vec<Header>>, PreflightError> {
        if method != "OPTIONS" || !requests_method {
            return Ok(None);
        }
        let Some(origin) = origin.filter(|value| !value.is_empty()) else {
            return Ok(None);
        };
        if self.any_origin || self.origins.iter().any(|allowed| allowed == origin) {
            Ok(Some(self.preflight.clone()))
        } else {
            Err(PreflightError::OriginDenied)
        }
    }
}
