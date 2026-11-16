//! The CORS headers every answer carries once a CORS policy is configured.
//! Mirrors `CorsPolicy.headers` in `hinoki.runtime.origins`; a Python test pins
//! the two together.

use crate::headers::Header;

#[derive(Debug, Clone, Default)]
pub struct Cors {
    pub any_origin: bool,
    /// Allowed origins, lower case.
    pub origins: Vec<Vec<u8>>,
    pub credentials: bool,
    pub expose: Vec<u8>,
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
}

#[cfg(test)]
mod tests {
    use super::*;

    fn listed() -> Cors {
        Cors {
            origins: vec![b"https://a.example".to_vec()],
            credentials: true,
            expose: b"x-total".to_vec(),
            ..Cors::default()
        }
    }

    #[test]
    fn listed_origin_gets_permission_and_vary() {
        let names: Vec<_> = listed()
            .headers(b"https://a.example")
            .into_iter()
            .map(|(n, _)| String::from_utf8(n).unwrap())
            .collect();
        assert_eq!(
            names,
            [
                "access-control-allow-origin",
                "vary",
                "access-control-allow-credentials",
                "access-control-expose-headers"
            ]
        );
    }

    #[test]
    fn unlisted_origin_only_varies() {
        assert_eq!(
            listed().headers(b"https://evil.example"),
            vec![(b"vary".to_vec(), b"origin".to_vec())]
        );
    }

    #[test]
    fn any_origin_answers_only_when_an_origin_was_sent() {
        let any = Cors {
            any_origin: true,
            ..Cors::default()
        };
        assert!(any.headers(b"").is_empty());
        assert_eq!(any.headers(b"https://x").len(), 1);
    }
}
