//! The mailer: renders a draft, hands it to a transport and keeps the counts.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};

use time::OffsetDateTime;
use tokio::sync::Semaphore;
use tokio::time::{timeout_at, Instant};

use crate::config::{Backend, Config};
use crate::error::{ConfigError, Kind, MailError};
use crate::limits::Limits;
use crate::message::{render, Draft, IdSource, RenderContext, Rendered, SystemIds};
use crate::observer::Observer;
use crate::transport::{CaptureTransport, SmtpTransport, Transport};

/// Messages larger than this are rendered on a blocking thread so encoding does
/// not stall the event loop.
const INLINE_RENDER_BYTES: usize = 64 * 1024;

/// A delivered message.
#[derive(Debug)]
pub struct Sent {
    pub message_id: String,
    /// The envelope recipients, in order.
    pub recipients: Vec<String>,
    pub attempts: u32,
    /// The rendered message, kept only by capture backends.
    pub raw: Option<Vec<u8>>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct StatsSnapshot {
    pub sent: u64,
    pub failed: u64,
    pub retries: u64,
    pub connections_open: u32,
    pub connections_idle: u32,
}

#[derive(Default)]
struct Counters {
    sent: AtomicU64,
    failed: AtomicU64,
    retries: AtomicU64,
}

pub struct Mailer {
    transport: Box<dyn Transport>,
    limits: Limits,
    message_id_domain: String,
    timeout: std::time::Duration,
    ids: Arc<dyn IdSource>,
    gate: Arc<Semaphore>,
    counters: Arc<Counters>,
    observer: OnceLock<Arc<dyn Observer>>,
    closed: AtomicBool,
}

impl Mailer {
    pub fn new(config: Config) -> Result<Self, ConfigError> {
        config.validate()?;
        let (transport, gate): (Box<dyn Transport>, usize) = match config.backend {
            Backend::Smtp(smtp) => {
                let permits = smtp.pool.max_connections;
                (Box::new(SmtpTransport::new(*smtp)?), permits)
            }
            Backend::Capture => (Box::new(CaptureTransport), 1),
        };
        Ok(Self::with_parts(
            transport,
            config.limits,
            config.message_id_domain,
            config.timeout,
            Arc::new(SystemIds),
            gate,
        ))
    }

    pub(crate) fn with_parts(
        transport: Box<dyn Transport>,
        limits: Limits,
        message_id_domain: String,
        timeout: std::time::Duration,
        ids: Arc<dyn IdSource>,
        gate: usize,
    ) -> Self {
        Self {
            transport,
            limits,
            message_id_domain,
            timeout,
            ids,
            gate: Arc::new(Semaphore::new(gate)),
            counters: Arc::new(Counters::default()),
            observer: OnceLock::new(),
            closed: AtomicBool::new(false),
        }
    }

    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    /// Attach the observer; false when one is attached already.
    pub fn attach(&self, observer: Arc<dyn Observer>) -> bool {
        self.observer.set(observer).is_ok()
    }

    pub fn stats(&self) -> StatsSnapshot {
        let (open, idle) = self.transport.connections();
        StatsSnapshot {
            sent: self.counters.sent.load(Ordering::Relaxed),
            failed: self.counters.failed.load(Ordering::Relaxed),
            retries: self.counters.retries.load(Ordering::Relaxed),
            connections_open: open,
            connections_idle: idle,
        }
    }

    /// Send `draft` within the configured timeout.
    pub async fn send(&self, draft: Draft) -> Result<Sent, MailError> {
        let started = Instant::now();
        let result = self.deliver(draft, started).await;
        let seconds = started.elapsed().as_secs_f64();
        match &result {
            Ok(_) => {
                self.counters.sent.fetch_add(1, Ordering::Relaxed);
                if let Some(observer) = self.observer.get() {
                    observer.sent(seconds);
                }
            }
            Err(error) => self.record_failure(error, seconds),
        }
        result
    }

    /// Count a message the caller refused before it reached [`Mailer::send`]
    /// (bad address, over a limit) like any other failed send, and return the
    /// error.
    pub fn refuse(&self, error: MailError) -> MailError {
        self.record_failure(&error, 0.0);
        error
    }

    fn record_failure(&self, error: &MailError, seconds: f64) {
        self.counters.failed.fetch_add(1, Ordering::Relaxed);
        if let Some(observer) = self.observer.get() {
            observer.failed(error.kind, seconds);
        }
    }

    async fn deliver(&self, draft: Draft, started: Instant) -> Result<Sent, MailError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(MailError::new(Kind::Closed));
        }
        let deadline = started + self.timeout;
        let rendered = self.render(draft, deadline).await?;
        let on_retry = || {
            self.counters.retries.fetch_add(1, Ordering::Relaxed);
            if let Some(observer) = self.observer.get() {
                observer.retried();
            }
        };
        let delivery = self.transport.send(&rendered, deadline, &on_retry).await?;
        let recipients = rendered
            .envelope
            .to
            .iter()
            .map(|address| address.as_str().to_owned())
            .collect();
        let raw = self.transport.keeps_raw().then_some(rendered.bytes);
        Ok(Sent {
            message_id: rendered.message_id,
            recipients,
            attempts: delivery.attempts,
            raw,
        })
    }

    fn context(&self) -> RenderContext {
        RenderContext {
            message_id_domain: self.message_id_domain.clone(),
            now: OffsetDateTime::now_utc(),
            ids: Arc::clone(&self.ids),
        }
    }

    async fn render(&self, draft: Draft, deadline: Instant) -> Result<Rendered, MailError> {
        let size = draft.text.as_ref().map_or(0, String::len)
            + draft.html.as_ref().map_or(0, String::len)
            + draft
                .attachments
                .iter()
                .map(|part| part.content.len())
                .sum::<usize>();
        let ctx = self.context();
        if size <= INLINE_RENDER_BYTES {
            return render(draft, &ctx, &self.limits);
        }
        // Bound the number of concurrent large renders like the connections.
        let permit = timeout_at(deadline, Arc::clone(&self.gate).acquire_owned())
            .await
            .map_err(|_| MailError::new(Kind::Timeout))?
            .map_err(|_| MailError::new(Kind::Closed))?;
        let limits = self.limits;
        let task = tokio::task::spawn_blocking(move || {
            let rendered = render(draft, &ctx, &limits);
            drop(permit);
            rendered
        });
        task.await.map_err(|_| MailError::new(Kind::Protocol))?
    }

    /// Check that the transport can deliver (SMTP: connect and authenticate).
    pub async fn check(&self) -> Result<(), MailError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(MailError::new(Kind::Closed));
        }
        self.transport.check(Instant::now() + self.timeout).await
    }

    /// Refuse new sends and close the idle connections. Idempotent.
    pub async fn close(&self) {
        self.closed.store(true, Ordering::Release);
        self.gate.close();
        self.transport.close().await;
    }
}

impl std::fmt::Debug for Mailer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Mailer").finish_non_exhaustive()
    }
}
