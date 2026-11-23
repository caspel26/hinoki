//! Where a rendered message goes: an SMTP server, or nowhere (the capture
//! backend for development and tests, where the host keeps the bytes).

use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use tokio::task::JoinSet;
use tokio::time::{sleep, timeout, Instant};
use tokio_rustls::TlsConnector;

use crate::config::SmtpConfig;
use crate::error::{ConfigError, Kind, MailError};
use crate::message::Rendered;
use crate::pool::{Lease, Pool};
use crate::retry::jitter;
use crate::smtp::{open, Connection, Failure};
use crate::tls::connector;

/// How long closing waits for the idle connections to say goodbye.
const CLOSE_TIMEOUT: Duration = Duration::from_secs(2);

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Delivery {
    /// Attempts made, the successful one included.
    pub attempts: u32,
}

/// A way to deliver a rendered message. Implementations must be cancel safe:
/// dropping a future returned by `send` leaves nothing half done.
pub trait Transport: Send + Sync {
    /// Deliver `message` before `deadline`. `on_retry` is called before each
    /// new attempt after a failed one.
    fn send<'a>(
        &'a self,
        message: &'a Rendered,
        deadline: Instant,
        on_retry: &'a (dyn Fn() + Send + Sync),
    ) -> BoxFuture<'a, Result<Delivery, MailError>>;

    /// Check that deliveries can work (connect and authenticate, for SMTP).
    fn check(&self, deadline: Instant) -> BoxFuture<'_, Result<(), MailError>>;

    /// Refuse new deliveries and release the connections.
    fn close(&self) -> BoxFuture<'_, ()>;

    /// Connections that exist and connections that are idle.
    fn connections(&self) -> (u32, u32);

    /// Whether the host gets the rendered bytes back (and so should keep them).
    fn keeps_raw(&self) -> bool;
}

/// Accepts every message without delivering it.
pub struct CaptureTransport;

impl Transport for CaptureTransport {
    fn send<'a>(
        &'a self,
        _: &'a Rendered,
        _: Instant,
        _: &'a (dyn Fn() + Send + Sync),
    ) -> BoxFuture<'a, Result<Delivery, MailError>> {
        Box::pin(async { Ok(Delivery { attempts: 1 }) })
    }

    fn check(&self, _: Instant) -> BoxFuture<'_, Result<(), MailError>> {
        Box::pin(async { Ok(()) })
    }

    fn close(&self) -> BoxFuture<'_, ()> {
        Box::pin(async {})
    }

    fn connections(&self) -> (u32, u32) {
        (0, 0)
    }

    fn keeps_raw(&self) -> bool {
        true
    }
}

pub struct SmtpTransport {
    config: SmtpConfig,
    pool: Pool<Connection>,
    tls: TlsConnector,
}

/// A reused connection that failed before any command worked was probably
/// closed by the server while idle.
fn is_stale(failure: &Failure) -> bool {
    let error = &failure.error;
    !failure.progressed
        && !error.possibly_sent
        && (error.kind == Kind::Protocol
            || (error.kind == Kind::Temporary && error.code == Some(421)))
}

/// Return the connection to the pool when it is still good, and report the
/// outcome.
fn finish(lease: Lease<Connection>, result: Result<(), Failure>) -> Result<(), MailError> {
    match result {
        Ok(()) => {
            lease.finish(true);
            Ok(())
        }
        Err(failure) => {
            if failure.reusable {
                lease.finish(false);
            }
            Err(failure.error)
        }
    }
}

impl SmtpTransport {
    pub fn new(config: SmtpConfig) -> Result<Self, ConfigError> {
        let tls = connector(config.tls_ca_file.as_deref())?;
        Ok(Self {
            pool: Pool::new(config.pool),
            config,
            tls,
        })
    }

    async fn checkout(&self, deadline: Instant) -> Result<Lease<Connection>, MailError> {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let wait = self.config.pool.wait;
        if remaining.is_zero() {
            return Err(MailError::new(Kind::Timeout));
        }
        match self.pool.checkout(wait.min(remaining)).await {
            Err(error) if error.kind == Kind::Busy && remaining < wait => {
                Err(MailError::new(Kind::Timeout))
            }
            other => other,
        }
    }

    /// One attempt: use an idle connection if there is one (and a fresh one if
    /// that turns out to be dead), else open a connection.
    async fn attempt(&self, message: &Rendered, deadline: Instant) -> Result<(), MailError> {
        let mut lease = self.checkout(deadline).await?;
        if let Some(conn) = lease.conn_mut() {
            match conn.send(message, deadline).await {
                Err(failure) if is_stale(&failure) => {}
                result => return finish(lease, result),
            }
        }
        lease.discard();
        let conn = open(&self.config, &self.tls, deadline).await?;
        let result = lease.attach(conn).send(message, deadline).await;
        finish(lease, result)
    }

    async fn deliver(
        &self,
        message: &Rendered,
        deadline: Instant,
        on_retry: &(dyn Fn() + Send + Sync),
    ) -> Result<Delivery, MailError> {
        let policy = self.config.retry;
        let mut attempts = 0;
        loop {
            attempts += 1;
            let mut error = match self.attempt(message, deadline).await {
                Ok(()) => return Ok(Delivery { attempts }),
                Err(error) => error,
            };
            error.attempts = attempts;
            let wait = policy.backoff(attempts + 1, jitter());
            if !policy.should_retry(&error, attempts) || Instant::now() + wait >= deadline {
                return Err(error);
            }
            on_retry();
            sleep(wait).await;
        }
    }

    async fn probe(&self, deadline: Instant) -> Result<(), MailError> {
        let mut lease = self.checkout(deadline).await?;
        // An idle connection proves nothing about the server now: open a new one.
        lease.discard();
        let mut conn = open(&self.config, &self.tls, deadline).await?;
        conn.quit().await;
        Ok(())
    }

    async fn shutdown(&self) {
        let idle = self.pool.close();
        let goodbyes = async {
            let mut set = JoinSet::new();
            for mut conn in idle {
                set.spawn(async move { conn.conn_mut().quit().await });
            }
            while set.join_next().await.is_some() {}
        };
        let _ = timeout(CLOSE_TIMEOUT, goodbyes).await;
    }
}

impl Transport for SmtpTransport {
    fn send<'a>(
        &'a self,
        message: &'a Rendered,
        deadline: Instant,
        on_retry: &'a (dyn Fn() + Send + Sync),
    ) -> BoxFuture<'a, Result<Delivery, MailError>> {
        Box::pin(self.deliver(message, deadline, on_retry))
    }

    fn check(&self, deadline: Instant) -> BoxFuture<'_, Result<(), MailError>> {
        Box::pin(self.probe(deadline))
    }

    fn close(&self) -> BoxFuture<'_, ()> {
        Box::pin(self.shutdown())
    }

    fn connections(&self) -> (u32, u32) {
        self.pool.counts()
    }

    fn keeps_raw(&self) -> bool {
        false
    }
}
