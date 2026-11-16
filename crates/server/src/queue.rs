//! The handoff between the tokio side and the asyncio loop.
//!
//! [`Queue`] is bounded; `push` reports whether the queue went from empty to
//! non-empty, which is the only moment the wake fd must be written. [`InFlight`]
//! bounds the requests between acceptance and response; over the bound the
//! caller answers 503 natively.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

#[derive(Debug, PartialEq, Eq)]
pub enum Push {
    /// Queued; the queue was empty, so the consumer must be woken.
    Wake,
    /// Queued behind other items; a wake is already pending.
    Queued,
}

#[derive(Debug)]
pub struct Queue<T> {
    items: Mutex<VecDeque<T>>,
    capacity: usize,
}

impl<T> Queue<T> {
    pub fn new(capacity: usize) -> Self {
        Self {
            items: Mutex::new(VecDeque::with_capacity(capacity.min(1024))),
            capacity,
        }
    }

    fn lock(&self) -> MutexGuard<'_, VecDeque<T>> {
        // A poisoned lock only means another thread panicked mid-push/pop; the
        // deque itself is always structurally valid.
        self.items.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Queue `item`, or give it back when the queue is full.
    pub fn push(&self, item: T) -> Result<Push, T> {
        let mut items = self.lock();
        if items.len() >= self.capacity {
            return Err(item);
        }
        let was_empty = items.is_empty();
        items.push_back(item);
        Ok(if was_empty { Push::Wake } else { Push::Queued })
    }

    /// Take up to `max` items in arrival order. A consumer woken by a `Wake`
    /// must keep draining until this returns fewer items than `max` (the queue
    /// is empty) before it waits again: items left behind produce no further
    /// wake, because `push` reports `Wake` only on the empty-to-non-empty edge.
    pub fn drain(&self, max: usize) -> Vec<T> {
        let mut items = self.lock();
        let count = max.min(items.len());
        items.drain(..count).collect()
    }

    pub fn len(&self) -> usize {
        self.lock().len()
    }

    pub fn is_empty(&self) -> bool {
        self.lock().is_empty()
    }
}

/// Counts requests in flight against a fixed bound.
#[derive(Debug, Clone)]
pub struct InFlight {
    count: Arc<AtomicUsize>,
    max: usize,
}

/// Releases its slot when dropped, on any thread and without the GIL.
#[derive(Debug)]
pub struct Permit {
    count: Arc<AtomicUsize>,
}

impl InFlight {
    pub fn new(max: usize) -> Self {
        Self {
            count: Arc::new(AtomicUsize::new(0)),
            max,
        }
    }

    pub fn try_acquire(&self) -> Option<Permit> {
        let mut taken = self.count.load(Ordering::Acquire);
        loop {
            if taken >= self.max {
                return None;
            }
            match self.count.compare_exchange_weak(
                taken,
                taken + 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => {
                    return Some(Permit {
                        count: Arc::clone(&self.count),
                    })
                }
                Err(seen) => taken = seen,
            }
        }
    }

    pub fn current(&self) -> usize {
        self.count.load(Ordering::Acquire)
    }
}

impl Drop for Permit {
    fn drop(&mut self) {
        self.count.fetch_sub(1, Ordering::AcqRel);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn only_the_first_push_wakes() {
        let queue = Queue::new(4);
        assert_eq!(queue.push(1), Ok(Push::Wake));
        assert_eq!(queue.push(2), Ok(Push::Queued));
        assert_eq!(queue.drain(10), vec![1, 2]);
        assert_eq!(queue.push(3), Ok(Push::Wake));
    }

    #[test]
    fn full_queue_returns_the_item() {
        let queue = Queue::new(2);
        queue.push("a").unwrap();
        queue.push("b").unwrap();
        assert_eq!(queue.push("c"), Err("c"));
        assert_eq!(queue.len(), 2);
    }

    #[test]
    fn drain_respects_the_batch_size_and_order() {
        let queue = Queue::new(10);
        for n in 0..5 {
            queue.push(n).unwrap();
        }
        assert_eq!(queue.drain(2), vec![0, 1]);
        assert_eq!(queue.drain(10), vec![2, 3, 4]);
        assert!(queue.is_empty());
    }

    #[test]
    fn in_flight_bound_and_release() {
        let gate = InFlight::new(2);
        let a = gate.try_acquire().unwrap();
        let _b = gate.try_acquire().unwrap();
        assert!(gate.try_acquire().is_none());
        drop(a);
        assert_eq!(gate.current(), 1);
        assert!(gate.try_acquire().is_some());
    }

    #[test]
    fn in_flight_never_exceeds_the_bound_across_threads() {
        let gate = InFlight::new(8);
        let workers: Vec<_> = (0..16)
            .map(|_| {
                let gate = gate.clone();
                thread::spawn(move || {
                    let mut peak = 0;
                    for _ in 0..2000 {
                        if let Some(permit) = gate.try_acquire() {
                            peak = peak.max(gate.current());
                            drop(permit);
                        }
                    }
                    peak
                })
            })
            .collect();
        for worker in workers {
            assert!(worker.join().unwrap() <= 8);
        }
        assert_eq!(gate.current(), 0);
    }

    #[test]
    fn concurrent_producers_lose_nothing() {
        let queue = Arc::new(Queue::new(10_000));
        let workers: Vec<_> = (0..4)
            .map(|id| {
                let queue = Arc::clone(&queue);
                thread::spawn(move || {
                    for n in 0..1000 {
                        queue.push(id * 1000 + n).unwrap();
                    }
                })
            })
            .collect();
        for worker in workers {
            worker.join().unwrap();
        }
        let mut all = queue.drain(10_000);
        all.sort_unstable();
        assert_eq!(all, (0..4000).collect::<Vec<_>>());
    }
}
