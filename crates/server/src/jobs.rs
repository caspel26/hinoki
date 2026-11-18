//! Bounded, tracked Rust jobs owned by one server core.

use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tokio::sync::{watch, Mutex, OnceCell};
use tokio::task::AbortHandle;
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
        self.try_spawn_with_handle(job).await.is_ok()
    }

    /// Start a bounded job and return a handle whose drop does not cancel it.
    /// The caller can explicitly abort it when its client request is cancelled.
    pub async fn try_spawn_with_handle<F>(&self, job: F) -> Result<AbortHandle, F>
    where
        F: Future<Output = ()> + Send + 'static,
    {
        let Some(permit) = self.in_flight.try_acquire() else {
            return Err(job);
        };
        let mut tasks = self.tasks.lock().await;
        while tasks.try_join_next().is_some() {}
        if !self.accepting.load(Ordering::Acquire) {
            return Err(job);
        }
        let task = tasks.spawn(async move {
            let _permit = permit;
            job.await;
        });
        Ok(task)
    }

    pub fn in_flight(&self) -> usize {
        self.in_flight.current()
    }

    #[cfg(test)]
    pub(crate) async fn tracked_tasks(&self) -> usize {
        self.tasks.lock().await.len()
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
        while tasks.try_join_next().is_some() {}
        let aborted = tasks.len();
        tasks.abort_all();
        while tasks.join_next().await.is_some() {}
        aborted
    }
}
