use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use crate::NativeJobs;

#[tokio::test]
async fn admission_is_held_until_a_job_finishes_or_is_aborted() {
    let jobs = Arc::new(NativeJobs::new(1));
    let started = Arc::new(AtomicBool::new(false));
    let signal = Arc::clone(&started);
    assert!(
        jobs.spawn(async move {
            signal.store(true, Ordering::Release);
            tokio::time::sleep(Duration::from_secs(30)).await;
        })
        .await
    );
    tokio::task::yield_now().await;
    assert!(started.load(Ordering::Acquire));
    assert_eq!(jobs.in_flight(), 1);
    assert!(!jobs.spawn(async {}).await);

    assert_eq!(jobs.drain(Duration::from_millis(1)).await, 1);
    assert_eq!(jobs.in_flight(), 0);
    assert!(!jobs.spawn(async {}).await);
}

#[tokio::test]
async fn shutdown_waits_for_jobs_that_finish_within_the_budget() {
    let jobs = Arc::new(NativeJobs::new(1));
    assert!(
        jobs.spawn(async {
            tokio::time::sleep(Duration::from_millis(1)).await;
        })
        .await
    );

    assert_eq!(jobs.drain(Duration::from_secs(1)).await, 0);
    assert_eq!(jobs.in_flight(), 0);
}

#[tokio::test]
async fn cancelling_the_drain_waiter_does_not_cancel_shutdown() {
    let jobs = Arc::new(NativeJobs::new(1));
    assert!(
        jobs.spawn(async {
            std::future::pending::<()>().await;
        })
        .await
    );
    let draining = Arc::clone(&jobs);
    let waiter = tokio::spawn(async move { draining.drain(Duration::from_millis(5)).await });
    tokio::task::yield_now().await;
    waiter.abort();
    let _ = waiter.await;
    tokio::time::sleep(Duration::from_millis(20)).await;

    assert_eq!(jobs.in_flight(), 0);
    assert!(!jobs.spawn(async {}).await);
    assert_eq!(jobs.drain(Duration::from_secs(1)).await, 1);
}
