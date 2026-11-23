//! A bounded pool of connections: a semaphore limits how many exist and an idle
//! list keeps the reusable ones. A connection goes back to the idle list only
//! through an explicit [`Lease::finish`]; dropping a lease closes it, so a
//! half-finished exchange (an error, a timeout, a cancelled task) is never
//! reused. There is no background task: idle connections expire when the pool
//! is next used and when it is closed.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::time::{timeout, Instant};

use crate::config::PoolConfig;
use crate::error::{Kind, MailError};

/// A connection that counts itself while it exists.
pub(crate) struct Counted<C> {
    conn: C,
    open: Arc<AtomicU32>,
}

impl<C> Counted<C> {
    pub(crate) fn conn_mut(&mut self) -> &mut C {
        &mut self.conn
    }
}

impl<C> Drop for Counted<C> {
    fn drop(&mut self) {
        self.open.fetch_sub(1, Ordering::Relaxed);
    }
}

struct Idle<C> {
    conn: Counted<C>,
    since: Instant,
    sent: u32,
}

struct Inner<C> {
    permits: Arc<Semaphore>,
    idle: Mutex<Vec<Idle<C>>>,
    config: PoolConfig,
    open: Arc<AtomicU32>,
    closed: AtomicBool,
}

impl<C> Inner<C> {
    fn idle(&self) -> MutexGuard<'_, Vec<Idle<C>>> {
        self.idle.lock().unwrap_or_else(|e| e.into_inner())
    }
}

pub(crate) struct Pool<C> {
    inner: Arc<Inner<C>>,
}

/// The right to use one connection slot, with the connection when one was idle.
pub(crate) struct Lease<C> {
    inner: Arc<Inner<C>>,
    _permit: OwnedSemaphorePermit,
    conn: Option<Counted<C>>,
    sent: u32,
    reused: bool,
}

impl<C> Pool<C> {
    pub(crate) fn new(config: PoolConfig) -> Self {
        Self {
            inner: Arc::new(Inner {
                permits: Arc::new(Semaphore::new(config.max_connections)),
                idle: Mutex::new(Vec::new()),
                config,
                open: Arc::new(AtomicU32::new(0)),
                closed: AtomicBool::new(false),
            }),
        }
    }

    /// Wait up to `wait` for a slot. `Busy` when none frees up in time,
    /// `Closed` when the pool is (or gets) closed.
    pub(crate) async fn checkout(&self, wait: Duration) -> Result<Lease<C>, MailError> {
        let inner = &self.inner;
        if inner.closed.load(Ordering::Acquire) {
            return Err(MailError::new(Kind::Closed));
        }
        let permit = match timeout(wait, Arc::clone(&inner.permits).acquire_owned()).await {
            Ok(Ok(permit)) => permit,
            Ok(Err(_)) => return Err(MailError::new(Kind::Closed)),
            Err(_) => return Err(MailError::new(Kind::Busy)),
        };
        let reusable = {
            let mut idle = inner.idle();
            idle.retain(|entry| entry.since.elapsed() < inner.config.idle_timeout);
            idle.pop()
        };
        let (conn, sent, reused) = match reusable {
            Some(entry) => (Some(entry.conn), entry.sent, true),
            None => (None, 0, false),
        };
        Ok(Lease {
            inner: Arc::clone(inner),
            _permit: permit,
            conn,
            sent,
            reused,
        })
    }

    /// Refuse new leases and return the idle connections. Waiting checkouts end
    /// with `Closed`; leases already out are closed when they are dropped.
    pub(crate) fn close(&self) -> Vec<Counted<C>> {
        self.inner.closed.store(true, Ordering::Release);
        self.inner.permits.close();
        let drained: Vec<Idle<C>> = std::mem::take(&mut *self.inner.idle());
        drained.into_iter().map(|entry| entry.conn).collect()
    }

    /// Connections that exist (idle or in use) and connections that are idle.
    pub(crate) fn counts(&self) -> (u32, u32) {
        let idle = u32::try_from(self.inner.idle().len()).unwrap_or(u32::MAX);
        (self.inner.open.load(Ordering::Relaxed), idle)
    }
}

impl<C> Lease<C> {
    /// The connection taken from the idle list, if any.
    pub(crate) fn conn_mut(&mut self) -> Option<&mut C> {
        self.conn.as_mut().map(Counted::conn_mut)
    }

    /// Whether the connection came from the idle list.
    #[cfg(test)]
    pub(crate) fn reused(&self) -> bool {
        self.reused
    }

    /// Close the connection and keep the slot.
    pub(crate) fn discard(&mut self) {
        self.conn = None;
        self.reused = false;
    }

    /// Put a freshly opened connection into the slot.
    pub(crate) fn attach(&mut self, conn: C) -> &mut C {
        self.inner.open.fetch_add(1, Ordering::Relaxed);
        self.sent = 0;
        self.reused = false;
        self.conn
            .insert(Counted {
                conn,
                open: Arc::clone(&self.inner.open),
            })
            .conn_mut()
    }

    /// Return the connection to the idle list, unless the pool is closed or the
    /// connection served its share of messages. `sent` counts a delivered
    /// message against that share.
    pub(crate) fn finish(mut self, sent: bool) {
        let Some(conn) = self.conn.take() else { return };
        let sent = self.sent + u32::from(sent);
        let mut idle = self.inner.idle();
        if self.inner.closed.load(Ordering::Acquire)
            || sent >= self.inner.config.max_messages_per_connection
        {
            return;
        }
        idle.push(Idle {
            conn,
            since: Instant::now(),
            sent,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(max: usize) -> PoolConfig {
        PoolConfig {
            max_connections: max,
            wait: Duration::from_millis(50),
            idle_timeout: Duration::from_secs(60),
            max_messages_per_connection: 3,
        }
    }

    const WAIT: Duration = Duration::from_millis(50);

    #[tokio::test]
    async fn slots_are_bounded_and_waiters_get_busy() {
        let pool: Pool<u8> = Pool::new(config(2));
        let first = pool.checkout(WAIT).await.unwrap();
        let _second = pool.checkout(WAIT).await.unwrap();
        assert_eq!(pool.checkout(WAIT).await.err().unwrap().kind, Kind::Busy);
        drop(first);
        assert!(pool.checkout(WAIT).await.is_ok());
    }

    #[tokio::test]
    async fn a_waiter_gets_the_slot_when_it_frees_up() {
        let pool = Arc::new(Pool::<u8>::new(config(1)));
        let held = pool.checkout(WAIT).await.unwrap();
        let waiter = {
            let pool = Arc::clone(&pool);
            tokio::spawn(async move { pool.checkout(Duration::from_secs(5)).await.is_ok() })
        };
        tokio::time::sleep(Duration::from_millis(20)).await;
        drop(held);
        assert!(waiter.await.unwrap());
    }

    #[tokio::test]
    async fn only_a_finished_lease_returns_its_connection() {
        let pool: Pool<u8> = Pool::new(config(2));
        let mut lease = pool.checkout(WAIT).await.unwrap();
        assert!(!lease.reused());
        assert!(lease.conn_mut().is_none());
        lease.attach(7);
        assert_eq!(pool.counts(), (1, 0));
        lease.finish(true);
        assert_eq!(pool.counts(), (1, 1));

        let mut lease = pool.checkout(WAIT).await.unwrap();
        assert!(lease.reused());
        assert_eq!(lease.conn_mut().copied(), Some(7));
        drop(lease);
        assert_eq!(pool.counts(), (0, 0));
    }

    #[tokio::test]
    async fn a_discarded_connection_is_closed_and_the_slot_kept() {
        let pool: Pool<u8> = Pool::new(config(1));
        let mut lease = pool.checkout(WAIT).await.unwrap();
        lease.attach(1);
        lease.discard();
        assert_eq!(pool.counts(), (0, 0));
        assert!(!lease.reused());
        lease.finish(true);
        assert_eq!(pool.counts(), (0, 0));
    }

    #[tokio::test]
    async fn a_connection_retires_after_its_message_budget() {
        let pool: Pool<u8> = Pool::new(config(1));
        let mut lease = pool.checkout(WAIT).await.unwrap();
        lease.attach(1);
        lease.finish(true);
        for _ in 0..2 {
            let lease = pool.checkout(WAIT).await.unwrap();
            assert!(lease.reused());
            lease.finish(true);
        }
        // Three messages used the budget of 3: the connection is gone.
        assert_eq!(pool.counts(), (0, 0));
        assert!(!pool.checkout(WAIT).await.unwrap().reused());
    }

    #[tokio::test]
    async fn a_connection_that_did_not_send_keeps_its_budget() {
        let pool: Pool<u8> = Pool::new(config(1));
        let mut lease = pool.checkout(WAIT).await.unwrap();
        lease.attach(1);
        for _ in 0..10 {
            lease.finish(false);
            lease = pool.checkout(WAIT).await.unwrap();
            assert!(lease.reused());
        }
    }

    #[tokio::test(start_paused = true)]
    async fn idle_connections_expire() {
        let pool: Pool<u8> = Pool::new(config(1));
        let mut lease = pool.checkout(WAIT).await.unwrap();
        lease.attach(1);
        lease.finish(true);
        tokio::time::advance(Duration::from_secs(59)).await;
        let lease = pool.checkout(WAIT).await.unwrap();
        assert!(lease.reused());
        lease.finish(false);
        tokio::time::advance(Duration::from_secs(61)).await;
        let lease = pool.checkout(WAIT).await.unwrap();
        assert!(!lease.reused());
        assert_eq!(pool.counts(), (0, 0));
    }

    #[tokio::test]
    async fn close_refuses_new_leases_wakes_waiters_and_returns_idle_connections() {
        let pool = Arc::new(Pool::<u8>::new(config(1)));
        let mut lease = pool.checkout(WAIT).await.unwrap();
        lease.attach(1);
        let waiter = {
            let pool = Arc::clone(&pool);
            tokio::spawn(async move {
                pool.checkout(Duration::from_secs(5))
                    .await
                    .err()
                    .map(|e| e.kind)
            })
        };
        tokio::time::sleep(Duration::from_millis(20)).await;
        let idle = pool.close();
        assert!(idle.is_empty());
        assert_eq!(waiter.await.unwrap(), Some(Kind::Closed));
        assert_eq!(pool.checkout(WAIT).await.err().unwrap().kind, Kind::Closed);
        // A lease out during close does not return its connection.
        lease.finish(true);
        assert_eq!(pool.counts(), (0, 0));
    }

    #[tokio::test]
    async fn close_hands_back_the_idle_connections() {
        let pool: Pool<u8> = Pool::new(config(2));
        let mut a = pool.checkout(WAIT).await.unwrap();
        let mut b = pool.checkout(WAIT).await.unwrap();
        a.attach(1);
        b.attach(2);
        a.finish(true);
        b.finish(true);
        let mut idle = pool.close();
        assert_eq!(idle.len(), 2);
        assert_eq!(pool.counts(), (2, 0));
        let values: Vec<u8> = idle.iter_mut().map(|c| *c.conn_mut()).collect();
        assert!(values.contains(&1) && values.contains(&2));
        drop(idle);
        assert_eq!(pool.counts(), (0, 0));
    }
}
