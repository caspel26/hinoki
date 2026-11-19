use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use crate::{Early, NativeJobs, Queue};

#[derive(Clone, Copy)]
enum StubCommand {
    Sleep,
    Fail,
    Defer,
    ErrorFinish,
    Replay,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum StubResult {
    Complete,
    Failed,
    Deferred,
    ErrorQueued,
    Replayed,
    Overloaded(Early),
}

async fn execute_stub(
    jobs: &Arc<NativeJobs>,
    queue: Arc<Queue<StubCommand>>,
    command: StubCommand,
) -> StubResult {
    let (sender, receiver) = tokio::sync::oneshot::channel();
    if !jobs
        .spawn(async move {
            let result = match command {
                StubCommand::Sleep => {
                    tokio::time::sleep(Duration::from_millis(1)).await;
                    StubResult::Complete
                }
                StubCommand::Fail => StubResult::Failed,
                StubCommand::Defer => cold_path(&queue, command, StubResult::Deferred),
                StubCommand::ErrorFinish => cold_path(&queue, command, StubResult::ErrorQueued),
                StubCommand::Replay => cold_path(&queue, command, StubResult::Replayed),
            };
            let _ = sender.send(result);
        })
        .await
    {
        return StubResult::Overloaded(Early::Overloaded);
    }
    receiver.await.unwrap_or(StubResult::Failed)
}

fn cold_path(queue: &Queue<StubCommand>, command: StubCommand, queued: StubResult) -> StubResult {
    match queue.push_cold_path(command) {
        Ok(_) => queued,
        Err(early) => StubResult::Overloaded(early),
    }
}

#[tokio::test]
async fn command_stub_covers_completion_failure_and_cold_paths() {
    let jobs = Arc::new(NativeJobs::new(2));
    let queue = Arc::new(Queue::new(3));

    assert_eq!(
        execute_stub(&jobs, Arc::clone(&queue), StubCommand::Sleep).await,
        StubResult::Complete
    );
    assert_eq!(
        execute_stub(&jobs, Arc::clone(&queue), StubCommand::Fail).await,
        StubResult::Failed
    );
    assert_eq!(
        execute_stub(&jobs, Arc::clone(&queue), StubCommand::Defer).await,
        StubResult::Deferred
    );
    assert_eq!(
        execute_stub(&jobs, Arc::clone(&queue), StubCommand::ErrorFinish).await,
        StubResult::ErrorQueued
    );
    assert_eq!(
        execute_stub(&jobs, Arc::clone(&queue), StubCommand::Replay).await,
        StubResult::Replayed
    );
    assert_eq!(queue.len(), 3);
    assert_eq!(jobs.in_flight(), 0);
}

#[tokio::test]
async fn every_full_queue_cold_path_answers_overloaded() {
    let jobs = Arc::new(NativeJobs::new(1));
    let queue = Arc::new(Queue::new(1));
    assert!(queue.push(StubCommand::Sleep).is_ok());

    for command in [
        StubCommand::Defer,
        StubCommand::ErrorFinish,
        StubCommand::Replay,
    ] {
        assert_eq!(
            execute_stub(&jobs, Arc::clone(&queue), command).await,
            StubResult::Overloaded(Early::Overloaded)
        );
    }
    assert_eq!(queue.len(), 1);
    assert_eq!(jobs.in_flight(), 0);
}

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
async fn a_dropped_client_receiver_does_not_cancel_the_native_job() {
    let jobs = Arc::new(NativeJobs::new(1));
    let (sender, receiver) = tokio::sync::oneshot::channel();
    drop(receiver);
    let finished = Arc::new(AtomicBool::new(false));
    let job_finished = Arc::clone(&finished);
    assert!(
        jobs.spawn(async move {
            tokio::time::sleep(Duration::from_millis(10)).await;
            job_finished.store(true, Ordering::Release);
            let _ = sender.send(());
        })
        .await
    );
    tokio::task::yield_now().await;
    assert_eq!(jobs.in_flight(), 1);
    assert_eq!(jobs.drain(Duration::from_secs(1)).await, 0);
    assert!(finished.load(Ordering::Acquire));
    assert_eq!(jobs.in_flight(), 0);
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
async fn completed_jobs_are_reaped_before_the_next_admission() {
    let jobs = Arc::new(NativeJobs::new(1));
    for _ in 0..3 {
        assert!(jobs.spawn(async {}).await);
        while jobs.in_flight() != 0 {
            tokio::task::yield_now().await;
        }
        assert_eq!(jobs.tracked_tasks().await, 1);
    }
    assert_eq!(jobs.tracked_tasks().await, 1);
    assert_eq!(jobs.drain(Duration::from_secs(1)).await, 0);
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
