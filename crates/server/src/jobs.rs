//! Bounded, tracked Rust jobs owned by one server core.

use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tokio::sync::{watch, Mutex, OnceCell};
use tokio::task::JoinSet;
use tokio::time::timeout;

use crate::InFlight;

/// Tracks jobs until completion or bounded shutdown. Each job owns its permit.
pub struct NativeJobs {
    accepting: AtomicBool,
    in_flight: InFlight,
    tasks: Mutex<JoinSet<()>>,
    drain_result: OnceCell<watch::Receiver<Option<usize>>>,
}

impl NativeJobs {
    pub fn new(max_in_flight: usize) -> Self {
        Self {
            accepting: AtomicBool::new(true),
            in_flight: InFlight::new(max_in_flight),
            tasks: Mutex::new(JoinSet::new()),
            drain_result: OnceCell::new(),
        }
    }

    /// Start a bounded job unless full or shutting down.
    pub async fn spawn<F>(&self, job: F) -> bool
    where
        F: Future<Output = ()> + Send + 'static,
    {
        let Some(permit) = self.in_flight.try_acquire() else {
            return false;
        };
        let mut tasks = self.tasks.lock().await;
        if !self.accepting.load(Ordering::Acquire) {
            return false;
        }
        tasks.spawn(async move {
            let _permit = permit;
            job.await;
        });
        true
    }

    pub fn in_flight(&self) -> usize {
        self.in_flight.current()
    }

    /// Stop admission, wait up to `budget`, then abort and join every survivor.
    pub async fn drain(self: &std::sync::Arc<Self>, budget: Duration) -> usize {
        let mut result = self
            .drain_result
            .get_or_init(|| async {
                self.accepting.store(false, Ordering::Release);
                let (sender, receiver) = watch::channel(None);
                let jobs = std::sync::Arc::clone(self);
                tokio::spawn(async move {
                    let count = jobs.drain_inner(budget).await;
                    sender.send_replace(Some(count));
                });
                receiver
            })
            .await
            .clone();
        loop {
            if let Some(count) = *result.borrow() {
                return count;
            }
            if result.changed().await.is_err() {
                return usize::MAX;
            }
        }
    }

    async fn drain_inner(&self, budget: Duration) -> usize {
        let mut tasks = self.tasks.lock().await;
        let finished = timeout(budget, async { while tasks.join_next().await.is_some() {} })
            .await
            .is_ok();
        if finished {
            return 0;
        }
        let aborted = tasks.len();
        tasks.abort_all();
        while tasks.join_next().await.is_some() {}
        aborted
    }
}
