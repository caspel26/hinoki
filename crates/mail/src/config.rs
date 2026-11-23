//! The settings of a mailer, as plain Rust values.

use std::fmt;
use std::net::IpAddr;
use std::path::PathBuf;
use std::time::Duration;

use crate::address::normalize_domain;
use crate::error::ConfigError;
use crate::limits::Limits;
use crate::retry::RetryPolicy;

pub const MAX_CONNECTIONS_CAP: usize = 64;
pub const MAX_MESSAGES_CAP: u32 = 10_000;
const MAX_DURATION: Duration = Duration::from_secs(3600);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Security {
    /// Plain connection upgraded with `STARTTLS` (required, not optional).
    StartTls,
    /// TLS from the first byte.
    Tls,
    /// No TLS at all.
    None,
}

/// A user name and password. `Debug` shows neither, and the password is
/// overwritten when this is dropped.
pub struct Credentials {
    pub username: String,
    pub password: String,
}

impl fmt::Debug for Credentials {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Credentials(***)")
    }
}

impl Drop for Credentials {
    fn drop(&mut self) {
        for secret in [&mut self.username, &mut self.password] {
            let mut bytes = std::mem::take(secret).into_bytes();
            bytes.fill(0);
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PoolConfig {
    pub max_connections: usize,
    /// How long a send waits for a free connection.
    pub wait: Duration,
    /// Idle connections older than this are closed when next looked at.
    pub idle_timeout: Duration,
    pub max_messages_per_connection: u32,
}

#[derive(Debug)]
pub struct SmtpConfig {
    pub host: String,
    pub port: u16,
    pub security: Security,
    pub credentials: Option<Credentials>,
    /// Allow a plain connection (or plain `AUTH`) to a host that is not on the
    /// loopback interface.
    pub allow_insecure: bool,
    /// Extra PEM roots, added to the bundled public ones.
    pub tls_ca_file: Option<PathBuf>,
    /// The `EHLO` name; the local address as a literal when `None`.
    pub helo_name: Option<String>,
    /// TCP connect plus TLS handshake.
    pub connect_timeout: Duration,
    /// Each command and its reply.
    pub command_timeout: Duration,
    /// Writing the message and reading the reply to it.
    pub data_timeout: Duration,
    pub pool: PoolConfig,
    pub retry: RetryPolicy,
}

#[derive(Debug)]
pub enum Backend {
    Smtp(Box<SmtpConfig>),
    /// Accepts every message; the host keeps or prints the rendered bytes.
    Capture,
}

#[derive(Debug)]
pub struct Config {
    pub backend: Backend,
    pub limits: Limits,
    /// The domain of generated `Message-ID` values.
    pub message_id_domain: String,
    /// The deadline of one send, from the call to the answer.
    pub timeout: Duration,
}

pub(crate) fn is_loopback_host(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost")
        || host
            .trim_matches(['[', ']'])
            .parse::<IpAddr>()
            .is_ok_and(|ip| ip.is_loopback())
}

/// Whether `text` is a domain in the normal form of [`normalize_domain`]: safe
/// to put on an `EHLO` line or in a `Message-ID`.
pub(crate) fn valid_domain_text(text: &str) -> bool {
    normalize_domain(text).is_some_and(|normal| normal == text)
}

fn check_duration(value: Duration, message: &'static str) -> Result<(), ConfigError> {
    if value.is_zero() || value > MAX_DURATION {
        return Err(ConfigError(message));
    }
    Ok(())
}

impl SmtpConfig {
    /// The host to connect to and to check the certificate against: an IPv6
    /// literal written in brackets (`[::1]`) loses them.
    pub(crate) fn server_host(&self) -> &str {
        match self
            .host
            .strip_prefix('[')
            .and_then(|rest| rest.strip_suffix(']'))
        {
            Some(inner) if inner.parse::<std::net::Ipv6Addr>().is_ok() => inner,
            _ => &self.host,
        }
    }

    fn validate(&self, timeout: Duration) -> Result<(), ConfigError> {
        if self.host.is_empty() || self.host.len() > 253 || self.port == 0 {
            return Err(ConfigError("host or port is not valid"));
        }
        if self
            .host
            .chars()
            .any(|c| c.is_control() || c.is_whitespace())
        {
            return Err(ConfigError("host is not valid"));
        }
        if self.security == Security::None && !self.allow_insecure && !is_loopback_host(&self.host)
        {
            return Err(ConfigError(
                "security none to a non-loopback host needs allow_insecure",
            ));
        }
        if self
            .credentials
            .as_ref()
            .is_some_and(|c| c.username.is_empty())
        {
            return Err(ConfigError("username must not be empty"));
        }
        if self
            .helo_name
            .as_deref()
            .is_some_and(|name| !valid_domain_text(name))
        {
            return Err(ConfigError("helo_name is not valid"));
        }
        self.validate_timeouts(timeout)?;
        self.validate_pool()?;
        self.retry.validate()
    }

    fn validate_timeouts(&self, timeout: Duration) -> Result<(), ConfigError> {
        check_duration(
            self.connect_timeout,
            "connect_timeout must be > 0 and <= 1 h",
        )?;
        check_duration(
            self.command_timeout,
            "command_timeout must be > 0 and <= 1 h",
        )?;
        check_duration(self.data_timeout, "data_timeout must be > 0 and <= 1 h")?;
        if timeout < self.connect_timeout {
            return Err(ConfigError("timeout must be at least connect_timeout"));
        }
        Ok(())
    }

    fn validate_pool(&self) -> Result<(), ConfigError> {
        let pool = &self.pool;
        if !(1..=MAX_CONNECTIONS_CAP).contains(&pool.max_connections) {
            return Err(ConfigError("max_connections must be between 1 and 64"));
        }
        if !(1..=MAX_MESSAGES_CAP).contains(&pool.max_messages_per_connection) {
            return Err(ConfigError(
                "max_messages_per_connection must be between 1 and 10000",
            ));
        }
        check_duration(pool.wait, "pool_wait must be > 0 and <= 1 h")?;
        check_duration(pool.idle_timeout, "idle_timeout must be > 0 and <= 1 h")
    }
}

impl Config {
    pub(crate) fn validate(&self) -> Result<(), ConfigError> {
        self.limits.validate()?;
        if !valid_domain_text(&self.message_id_domain) {
            return Err(ConfigError("message_id_domain is not valid"));
        }
        check_duration(self.timeout, "timeout must be > 0 and <= 1 h")?;
        match &self.backend {
            Backend::Smtp(smtp) => smtp.validate(self.timeout),
            Backend::Capture => Ok(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(crate) fn smtp() -> SmtpConfig {
        SmtpConfig {
            host: "127.0.0.1".to_owned(),
            port: 2525,
            security: Security::None,
            credentials: None,
            allow_insecure: false,
            tls_ca_file: None,
            helo_name: None,
            connect_timeout: Duration::from_secs(1),
            command_timeout: Duration::from_secs(1),
            data_timeout: Duration::from_secs(1),
            pool: PoolConfig {
                max_connections: 2,
                wait: Duration::from_secs(1),
                idle_timeout: Duration::from_secs(60),
                max_messages_per_connection: 10,
            },
            retry: RetryPolicy {
                retries: 1,
                base: Duration::from_millis(10),
                max: Duration::from_millis(20),
            },
        }
    }

    fn config(smtp: SmtpConfig) -> Config {
        Config {
            backend: Backend::Smtp(Box::new(smtp)),
            limits: Limits::default(),
            message_id_domain: "example.com".to_owned(),
            timeout: Duration::from_secs(5),
        }
    }

    #[test]
    fn credentials_never_show_in_debug_output() {
        let credentials = Credentials {
            username: "ada-user".to_owned(),
            password: "hunter2-secret".to_owned(),
        };
        let mut smtp = smtp();
        smtp.credentials = Some(credentials);
        let shown = format!("{:?}", config(smtp));
        assert!(!shown.contains("ada-user"));
        assert!(!shown.contains("hunter2"));
        assert!(shown.contains("Credentials(***)"));
    }

    #[test]
    fn loopback_hosts_are_recognized() {
        for host in [
            "localhost",
            "LOCALHOST",
            "127.0.0.1",
            "127.9.9.9",
            "::1",
            "[::1]",
        ] {
            assert!(is_loopback_host(host), "{host}");
        }
        for host in [
            "mail.example.com",
            "10.0.0.1",
            "::2",
            "localhost.example.com",
        ] {
            assert!(!is_loopback_host(host), "{host}");
        }
    }

    #[test]
    fn a_valid_configuration_passes() {
        assert_eq!(config(smtp()).validate(), Ok(()));
        let capture = Config {
            backend: Backend::Capture,
            ..config(smtp())
        };
        assert_eq!(capture.validate(), Ok(()));
    }

    #[test]
    fn invalid_settings_are_refused() {
        type Mutation = Box<dyn Fn(&mut SmtpConfig)>;
        let mutations: Vec<Mutation> = vec![
            Box::new(|s| s.host = String::new()),
            Box::new(|s| s.host = "a b".to_owned()),
            Box::new(|s| s.port = 0),
            Box::new(|s| s.host = "mail.example.com".to_owned()),
            Box::new(|s| s.helo_name = Some("bad name".to_owned())),
            Box::new(|s| s.helo_name = Some("a\r\nb".to_owned())),
            Box::new(|s| s.connect_timeout = Duration::ZERO),
            Box::new(|s| s.command_timeout = Duration::from_secs(3601)),
            Box::new(|s| s.data_timeout = Duration::ZERO),
            Box::new(|s| s.connect_timeout = Duration::from_secs(10)),
            Box::new(|s| s.pool.max_connections = 0),
            Box::new(|s| s.pool.max_connections = 65),
            Box::new(|s| s.pool.max_messages_per_connection = 0),
            Box::new(|s| s.pool.max_messages_per_connection = 10_001),
            Box::new(|s| s.pool.wait = Duration::ZERO),
            Box::new(|s| s.pool.idle_timeout = Duration::ZERO),
            Box::new(|s| s.retry.retries = 6),
            Box::new(|s| {
                s.credentials = Some(Credentials {
                    username: String::new(),
                    password: "x".to_owned(),
                })
            }),
        ];
        for mutate in mutations {
            let mut smtp = smtp();
            mutate(&mut smtp);
            assert!(config(smtp).validate().is_err());
        }
        let mut bad = config(smtp());
        bad.message_id_domain = "bad domain".to_owned();
        assert!(bad.validate().is_err());
        let mut bad = config(smtp());
        bad.timeout = Duration::ZERO;
        assert!(bad.validate().is_err());
        let mut bad = config(smtp());
        bad.limits.max_recipients = 0;
        assert!(bad.validate().is_err());
    }

    #[test]
    fn insecure_remote_hosts_need_an_explicit_opt_in() {
        let mut remote = smtp();
        remote.host = "mail.example.com".to_owned();
        remote.allow_insecure = true;
        assert_eq!(config(remote).validate(), Ok(()));
        let mut tls = smtp();
        tls.host = "mail.example.com".to_owned();
        tls.security = Security::StartTls;
        assert_eq!(config(tls).validate(), Ok(()));
    }

    #[test]
    fn domain_text_is_restricted() {
        assert!(valid_domain_text("mail.example.com"));
        assert!(valid_domain_text("localhost"));
        assert!(valid_domain_text("[127.0.0.1]"));
        assert!(valid_domain_text("[IPv6:::1]"));
        assert!(!valid_domain_text(""));
        assert!(!valid_domain_text("a b"));
        assert!(!valid_domain_text("a\r\nb"));
        assert!(!valid_domain_text("foo:bar[]"));
        assert!(!valid_domain_text("[::1]"));
        assert!(!valid_domain_text("[999.1.1.1]"));
        assert!(!valid_domain_text("-a.example.com"));
        assert!(!valid_domain_text("Example.com"));
        assert!(!valid_domain_text(&"a".repeat(254)));
    }

    #[test]
    fn a_bracketed_ipv6_host_is_connected_without_brackets() {
        let mut config = smtp();
        config.host = "[::1]".to_owned();
        assert_eq!(config.server_host(), "::1");
        config.host = "[not-an-ip]".to_owned();
        assert_eq!(config.server_host(), "[not-an-ip]");
        config.host = "mail.example.com".to_owned();
        assert_eq!(config.server_host(), "mail.example.com");
    }
}
