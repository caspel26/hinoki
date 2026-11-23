//! Outbound email for Hinoki: address validation, MIME rendering and an SMTP
//! client with a bounded connection pool. Pure Rust, no Python types.

#![forbid(unsafe_code)]

mod address;
mod config;
mod error;
mod header;
mod limits;
mod mailer;
mod message;
mod mime;
mod observer;
mod pool;
mod retry;
mod smtp;
mod template;
mod tls;
mod transport;

pub use address::{normalize_domain, parse_addr_spec, Address, Mailbox};
pub use config::{
    Backend, Config, Credentials, PoolConfig, Security, SmtpConfig, MAX_CONNECTIONS_CAP,
    MAX_MESSAGES_CAP,
};
pub use error::{ConfigError, Field, Kind, MailError};
pub use limits::Limits;
pub use mailer::{Mailer, Sent, StatsSnapshot};
pub use message::{
    mailboxes, render, AttachmentPart, Draft, Envelope, IdSource, RenderContext, Rendered,
    SystemIds,
};
pub use observer::Observer;
pub use retry::{RetryPolicy, MAX_RETRIES};
pub use template::{
    limits as template_limits, Map as TemplateMap, Part, Pos, Problem, Reason, Refusal,
    Rendered as RenderedTemplate, Shape, Template, TemplateError, Value,
};
