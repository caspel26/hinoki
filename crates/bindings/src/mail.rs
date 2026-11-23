//! Python bindings of the email backend: `Mailer`, built from a dict of
//! settings, and `MailMetrics`, the recorder the server hands out. Only plain
//! values cross; the sending itself runs on the shared tokio runtime.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use hinoki_mail::{
    mailboxes, normalize_domain, parse_addr_spec, AttachmentPart, Backend, Config, Credentials,
    Draft, Field, Kind, Limits, MailError, Mailbox, Mailer, Observer, PoolConfig, RetryPolicy,
    Security, Sent, SmtpConfig,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyString};

use crate::completion;
use crate::compression::field;

static EMAIL_ERROR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static EMAIL_ERROR_KIND: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

const OPTIONS: [&str; 27] = [
    "backend",
    "host",
    "port",
    "security",
    "username",
    "password",
    "allow_insecure",
    "tls_ca_file",
    "helo_name",
    "default_sender",
    "message_id_domain",
    "connect_timeout",
    "command_timeout",
    "data_timeout",
    "timeout",
    "max_connections",
    "pool_wait",
    "idle_timeout",
    "max_messages_per_connection",
    "retries",
    "backoff_base",
    "backoff_max",
    "max_recipients",
    "max_message_bytes",
    "max_attachments",
    // The two below are read by the Python side; they are accepted and ignored
    // so one settings dict can serve both.
    "memory_capacity",
    "check_on_startup",
];

fn invalid(message: String) -> PyErr {
    PyValueError::new_err(format!("email: {message}"))
}

/// The `hinoki.mail.EmailError` for `error`, built from its plain attributes.
fn email_error(py: Python<'_>, error: &MailError) -> PyErr {
    build_error(py, error).unwrap_or_else(|failure| failure)
}

fn build_error(py: Python<'_>, error: &MailError) -> PyResult<PyErr> {
    let class = EMAIL_ERROR.import(py, "hinoki.mail", "EmailError")?;
    let kinds = EMAIL_ERROR_KIND.import(py, "hinoki.mail", "EmailErrorKind")?;
    let kind = kinds.call1((error.kind.as_str(),))?;
    let details = PyDict::new(py);
    details.set_item("code", error.code)?;
    details.set_item("enhanced_status", error.enhanced.as_deref())?;
    details.set_item("attempts", error.attempts)?;
    details.set_item("possibly_sent", error.possibly_sent)?;
    details.set_item("field", error.field.map(Field::as_str))?;
    details.set_item("index", error.index)?;
    let instance = class.call((kind,), Some(&details))?;
    Ok(PyErr::from_value(instance))
}

fn check_keys(options: &Bound<'_, PyDict>) -> PyResult<()> {
    for key in options.keys() {
        let name: String = key.extract()?;
        if !OPTIONS.contains(&name.as_str()) {
            return Err(invalid(format!("unknown option {name:?}")));
        }
    }
    Ok(())
}

fn seconds(options: &Bound<'_, PyDict>, name: &str, default: f64) -> PyResult<Duration> {
    let value = field::<f64>(options, name)?.unwrap_or(default);
    Duration::try_from_secs_f64(value)
        .map_err(|_| invalid(format!("{name} must be a number of seconds >= 0")))
}

fn count<'py, T: FromPyObjectOwned<'py>>(
    options: &Bound<'py, PyDict>,
    name: &str,
    default: T,
) -> PyResult<T> {
    Ok(field::<T>(options, name)?.unwrap_or(default))
}

fn security_of(options: &Bound<'_, PyDict>) -> PyResult<Security> {
    match field::<String>(options, "security")?.as_deref() {
        None | Some("starttls") => Ok(Security::StartTls),
        Some("tls") => Ok(Security::Tls),
        Some("none") => Ok(Security::None),
        Some(_) => Err(invalid(
            "security must be \"starttls\", \"tls\" or \"none\"".to_owned(),
        )),
    }
}

fn credentials_of(options: &Bound<'_, PyDict>) -> PyResult<Option<Credentials>> {
    let username: Option<String> = field(options, "username")?;
    let password: Option<String> = field(options, "password")?;
    match (username, password) {
        (None, None) => Ok(None),
        (Some(username), Some(password)) => Ok(Some(Credentials { username, password })),
        _ => Err(invalid(
            "username and password must be set together".to_owned(),
        )),
    }
}

fn pool_of(options: &Bound<'_, PyDict>) -> PyResult<PoolConfig> {
    Ok(PoolConfig {
        max_connections: count(options, "max_connections", 4)?,
        wait: seconds(options, "pool_wait", 5.0)?,
        idle_timeout: seconds(options, "idle_timeout", 60.0)?,
        max_messages_per_connection: count(options, "max_messages_per_connection", 100)?,
    })
}

fn retry_of(options: &Bound<'_, PyDict>) -> PyResult<RetryPolicy> {
    Ok(RetryPolicy {
        retries: count(options, "retries", 2)?,
        base: seconds(options, "backoff_base", 0.5)?,
        max: seconds(options, "backoff_max", 5.0)?,
    })
}

fn smtp_of(options: &Bound<'_, PyDict>) -> PyResult<SmtpConfig> {
    let security = security_of(options)?;
    let default_port = match security {
        Security::StartTls => 587,
        Security::Tls => 465,
        Security::None => 25,
    };
    Ok(SmtpConfig {
        host: field(options, "host")?.unwrap_or_else(|| "localhost".to_owned()),
        port: field(options, "port")?.unwrap_or(default_port),
        security,
        credentials: credentials_of(options)?,
        allow_insecure: count(options, "allow_insecure", false)?,
        tls_ca_file: field::<String>(options, "tls_ca_file")?.map(PathBuf::from),
        helo_name: domain_of(options, "helo_name")?,
        connect_timeout: seconds(options, "connect_timeout", 10.0)?,
        command_timeout: seconds(options, "command_timeout", 30.0)?,
        data_timeout: seconds(options, "data_timeout", 120.0)?,
        pool: pool_of(options)?,
        retry: retry_of(options)?,
    })
}

fn limits_of(options: &Bound<'_, PyDict>) -> PyResult<Limits> {
    let defaults = Limits::default();
    Ok(Limits {
        max_recipients: count(options, "max_recipients", defaults.max_recipients)?,
        max_message_bytes: count(options, "max_message_bytes", defaults.max_message_bytes)?,
        max_attachments: count(options, "max_attachments", defaults.max_attachments)?,
    })
}

fn backend_of(options: &Bound<'_, PyDict>) -> PyResult<Backend> {
    match field::<String>(options, "backend")?.as_deref() {
        Some("smtp") => Ok(Backend::Smtp(Box::new(smtp_of(options)?))),
        Some("console" | "memory") => Ok(Backend::Capture),
        _ => Err(invalid(
            "backend must be \"smtp\", \"console\" or \"memory\"".to_owned(),
        )),
    }
}

/// A domain setting in its normal form (lower-case ASCII); `None` when unset.
fn domain_of(options: &Bound<'_, PyDict>, name: &str) -> PyResult<Option<String>> {
    field::<String>(options, name)?
        .map(|text| normalize_domain(&text).ok_or_else(|| invalid(format!("{name} is not valid"))))
        .transpose()
}

/// The domain of generated `Message-ID` values: the setting, else the domain
/// of `default_sender`, else `helo_name`, else `localhost`.
fn message_id_domain_of(options: &Bound<'_, PyDict>) -> PyResult<String> {
    if let Some(domain) = domain_of(options, "message_id_domain")? {
        return Ok(domain);
    }
    if let Some(domain) = default_sender_domain(options)? {
        return Ok(domain);
    }
    Ok(domain_of(options, "helo_name")?.unwrap_or_else(|| "localhost".to_owned()))
}

/// The (punycode) domain of `default_sender`; refuses an address that is not
/// valid.
fn default_sender_domain(options: &Bound<'_, PyDict>) -> PyResult<Option<String>> {
    field::<String>(options, "default_sender")?
        .map(|sender| {
            parse_addr_spec(&sender)
                .map(|address| address.domain().to_owned())
                .map_err(|_| invalid("default_sender is not a valid address".to_owned()))
        })
        .transpose()
}

fn config_of(options: &Bound<'_, PyDict>) -> PyResult<Config> {
    Ok(Config {
        backend: backend_of(options)?,
        limits: limits_of(options)?,
        message_id_domain: message_id_domain_of(options)?,
        timeout: seconds(options, "timeout", 60.0)?,
    })
}

/// A recipient as Python passes it: the address and an optional display name.
type Recipient = (String, Option<String>);

/// What `Mailer.send` takes: a dict with these keys.
#[derive(FromPyObject)]
struct Message<'py> {
    #[pyo3(item)]
    sender: Recipient,
    #[pyo3(item)]
    to: Vec<Recipient>,
    #[pyo3(item)]
    cc: Vec<Recipient>,
    #[pyo3(item)]
    bcc: Vec<Recipient>,
    #[pyo3(item)]
    reply_to: Vec<Recipient>,
    #[pyo3(item)]
    subject: String,
    #[pyo3(item)]
    text: Option<Bound<'py, PyString>>,
    #[pyo3(item)]
    html: Option<Bound<'py, PyString>>,
    /// (filename, content type, content)
    #[pyo3(item)]
    attachments: Vec<(String, String, Bound<'py, PyBytes>)>,
    /// (name, value)
    #[pyo3(item)]
    headers: Vec<(String, String)>,
}

/// A message with the content copied out of Python, addresses not yet checked.
struct Parts {
    sender: Recipient,
    lists: [(Field, Vec<Recipient>); 4],
    subject: String,
    text: Option<String>,
    html: Option<String>,
    attachments: Vec<AttachmentPart>,
    headers: Vec<(String, String)>,
}

fn utf8_len(text: Option<&Bound<'_, PyString>>) -> PyResult<usize> {
    text.map_or(Ok(0), |text| Ok(text.to_str()?.len()))
}

impl Message<'_> {
    /// Refuse a message over the count and size limits before its bytes are
    /// copied.
    fn check(&self, limits: &Limits) -> PyResult<Result<(), MailError>> {
        let recipients = self.to.len() + self.cc.len() + self.bcc.len();
        let bytes = self
            .attachments
            .iter()
            .map(|(_, _, content)| content.as_bytes().len())
            .fold(0usize, usize::saturating_add);
        Ok(limits.check_inputs(
            recipients,
            self.attachments.len(),
            bytes,
            utf8_len(self.text.as_ref())?,
            utf8_len(self.html.as_ref())?,
        ))
    }

    fn into_parts(self) -> PyResult<Parts> {
        Ok(Parts {
            sender: self.sender,
            lists: [
                (Field::To, self.to),
                (Field::Cc, self.cc),
                (Field::Bcc, self.bcc),
                (Field::ReplyTo, self.reply_to),
            ],
            subject: self.subject,
            text: self
                .text
                .map(|text| text.to_str().map(str::to_owned))
                .transpose()?,
            html: self
                .html
                .map(|html| html.to_str().map(str::to_owned))
                .transpose()?,
            attachments: self
                .attachments
                .into_iter()
                .map(|(filename, content_type, content)| AttachmentPart {
                    filename,
                    content_type,
                    content: content.as_bytes().to_vec(),
                })
                .collect(),
            headers: self.headers,
        })
    }
}

/// A sender is one mailbox: its errors carry no position.
fn sender_of(sender: Recipient) -> Result<Mailbox, MailError> {
    let mut boxes = mailboxes(Field::Sender, vec![sender]).map_err(|mut error| {
        error.index = None;
        error
    })?;
    boxes
        .pop()
        .ok_or_else(|| MailError::at(Kind::InvalidMessage, Field::Sender, None))
}

impl Parts {
    fn into_draft(self) -> Result<Draft, MailError> {
        let [(_, to), (_, cc), (_, bcc), (_, reply_to)] = self.lists;
        Ok(Draft {
            sender: sender_of(self.sender)?,
            to: mailboxes(Field::To, to)?,
            cc: mailboxes(Field::Cc, cc)?,
            bcc: mailboxes(Field::Bcc, bcc)?,
            reply_to: mailboxes(Field::ReplyTo, reply_to)?,
            subject: self.subject,
            text: self.text,
            html: self.html,
            attachments: self.attachments,
            headers: self.headers,
        })
    }
}

fn result_of(py: Python<'_>, sent: Sent) -> PyResult<Py<PyAny>> {
    let raw = sent.raw.map(|bytes| PyBytes::new(py, &bytes));
    Ok((sent.message_id, sent.recipients, sent.attempts, raw)
        .into_pyobject(py)?
        .into_any()
        .unbind())
}

fn nothing(py: Python<'_>, (): ()) -> PyResult<Py<PyAny>> {
    Ok(py.None())
}

fn failed(py: Python<'_>, error: MailError) -> PyErr {
    email_error(py, &error)
}

fn panicked(py: Python<'_>) -> PyErr {
    email_error(py, &MailError::new(Kind::Protocol))
}

/// The outbound mailer of one worker. Create it in the lifespan, never at
/// import: it holds the connection pool.
#[pyclass(name = "Mailer", module = "hinoki.core", frozen)]
pub(crate) struct PyMailer {
    inner: Arc<Mailer>,
}

#[pymethods]
impl PyMailer {
    #[new]
    fn new(options: &Bound<'_, PyDict>) -> PyResult<Self> {
        check_keys(options)?;
        default_sender_domain(options)?;
        let mailer =
            Mailer::new(config_of(options)?).map_err(|error| invalid(error.to_string()))?;
        Ok(Self {
            inner: Arc::new(mailer),
        })
    }

    /// Send a message (a dict, see `Message`). Resolves to
    /// `(message_id, recipients, attempts, raw)`; `raw` is the rendered message
    /// for the console and memory backends and `None` for SMTP.
    fn send<'py>(&self, py: Python<'py>, message: Message<'py>) -> PyResult<Bound<'py, PyAny>> {
        crate::db::ensure_runtime();
        let mailer = Arc::clone(&self.inner);
        if let Err(error) = message.check(mailer.limits())? {
            return Err(email_error(py, &mailer.refuse(error)));
        }
        let parts = message.into_parts()?;
        let work = async move {
            match parts.into_draft() {
                Ok(draft) => mailer.send(draft).await,
                Err(error) => Err(mailer.refuse(error)),
            }
        };
        completion::submit_with(py, work, result_of, (failed, panicked))
    }

    /// Count a message Python refused before `send` (an error kind name, see
    /// `EmailErrorKind`) as a failed send.
    fn refuse(&self, kind: &str) -> PyResult<()> {
        let kind = Kind::ALL
            .into_iter()
            .find(|candidate| candidate.as_str() == kind)
            .ok_or_else(|| invalid(format!("unknown error kind {kind:?}")))?;
        self.inner.refuse(MailError::new(kind));
        Ok(())
    }

    /// Connect and authenticate with a new connection (SMTP); nothing is sent.
    fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        crate::db::ensure_runtime();
        let mailer = Arc::clone(&self.inner);
        let work = async move { mailer.check().await };
        completion::submit_with(py, work, nothing, (failed, panicked))
    }

    /// Refuse new sends and close the idle connections. Safe to call twice.
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        crate::db::ensure_runtime();
        let mailer = Arc::clone(&self.inner);
        let work = async move {
            mailer.close().await;
            Ok::<(), MailError>(())
        };
        completion::submit_with(py, work, nothing, (failed, panicked))
    }

    /// `(sent, failed, retries, connections_open, connections_idle)`.
    fn stats(&self) -> (u64, u64, u64, u32, u32) {
        let stats = self.inner.stats();
        (
            stats.sent,
            stats.failed,
            stats.retries,
            stats.connections_open,
            stats.connections_idle,
        )
    }

    /// Record sends into `metrics`; `False` when metrics are attached already.
    fn attach_metrics(&self, metrics: &MailMetrics) -> bool {
        self.inner.attach(Arc::new(Recorder {
            registry: Arc::clone(&metrics.registry),
        }))
    }
}

struct Recorder {
    registry: Arc<kiso::Registry>,
}

impl Observer for Recorder {
    fn sent(&self, seconds: f64) {
        self.registry.observe_email("sent", elapsed(seconds));
    }

    fn failed(&self, kind: Kind, seconds: f64) {
        self.registry.observe_email(kind.as_str(), elapsed(seconds));
    }

    fn retried(&self) {
        self.registry.observe_email_retry();
    }
}

fn elapsed(seconds: f64) -> Duration {
    Duration::try_from_secs_f64(seconds).unwrap_or(Duration::ZERO)
}

/// Where a mailer records its sends: the fixed email series of the server's
/// metrics registry.
#[pyclass(name = "MailMetrics", module = "hinoki.core", frozen)]
pub(crate) struct MailMetrics {
    registry: Arc<kiso::Registry>,
}

impl MailMetrics {
    pub(crate) fn new(registry: Arc<kiso::Registry>) -> Self {
        Self { registry }
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMailer>()?;
    m.add_class::<MailMetrics>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use kiso::metrics::{Config as MetricsConfig, EMAIL_OUTCOMES};

    #[test]
    fn the_metric_outcomes_are_sent_and_every_error_kind() {
        let mut expected = vec!["sent"];
        expected.extend(Kind::ALL.iter().map(|kind| kind.as_str()));
        assert_eq!(EMAIL_OUTCOMES.to_vec(), expected);
    }

    #[test]
    fn the_recorder_counts_sends_failures_and_retries() {
        let config = MetricsConfig {
            email: Some("smtp".to_owned()),
            ..MetricsConfig::default()
        };
        let registry = Arc::new(kiso::Registry::new(config, &[]).unwrap());
        let recorder = Recorder {
            registry: Arc::clone(&registry),
        };
        recorder.sent(0.02);
        recorder.failed(Kind::Rejected, 0.5);
        recorder.failed(Kind::Busy, f64::NAN);
        recorder.retried();
        let text = registry.render();
        for expected in [
            "outcome=\"sent\",worker=\"0\"} 1",
            "outcome=\"rejected\",worker=\"0\"} 1",
            "outcome=\"busy\",worker=\"0\"} 1",
            "hinoki_email_send_duration_seconds_count{backend=\"smtp\",worker=\"0\"} 3",
            "hinoki_email_retries_total{backend=\"smtp\",worker=\"0\"} 1",
        ] {
            assert!(text.contains(expected), "{expected}\n{text}");
        }
    }
}
