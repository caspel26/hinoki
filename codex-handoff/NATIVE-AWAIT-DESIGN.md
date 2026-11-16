# Native-await completion (build step 3b): local design, not committed

Status: **design only, nothing implemented.** Parent: `docs/RUST-SERVER-DESIGN.md` §3.3 and §9 step 3b.
Date: 2026-11-16. Author: Claude Code (Sonnet 5.5). Needs: the A/B gate in §7 before adoption.

## 1. Observed code (read, not assumed)

`crates/bindings/src/db.rs` wraps every ORM call in `bridge!`, which is
`pyo3_async_runtimes::tokio::future_into_py(py, async move { fut.await; Python::attach(convert) })`.
Reading `pyo3-async-runtimes-0.30.0/src/generic.rs` (`future_into_py_with_locals`, l.608-690):

1. The DB future runs on a tokio **worker** (`R::spawn`), wrapped in `Cancellable` (cancel via a
   `oneshot` fired by an asyncio done-callback).
2. `bridge!`'s `Python::attach(convert)` (rows → `dict`s, `db_error`) runs **inside that future, on
   the worker**, and waits for the GIL there.
3. After the future returns, the library does `R::spawn_blocking(|| Python::attach(...))`: a
   **blocking-pool** thread takes the GIL again, and calls `loop.call_soon_threadsafe(CheckedCompletor,
   future, set_result, value)`.
4. `call_soon_threadsafe` writes the loop's self-pipe; the loop runs `CheckedCompletor`, which sets
   the result and schedules the awaiting task.

**Correction to `RUST-SERVER-DESIGN.md` §3.3.** It says `future_into_py` "takes the GIL on a tokio
thread". It does not: step 3 uses `spawn_blocking`. The GIL wait on a tokio **worker** is only step
2 (the `bridge!` conversion). The mitigation below is still justified, but its size is smaller than
§3.3 implies and must be measured, not assumed.

Per ORM call today: 2 GIL acquisitions by foreign threads (worker + blocking-pool), 1 thread hop to
the blocking pool, 1 `call_soon_threadsafe` (self-pipe write + a `CheckedCompletor` call + a
`set_result` call through the loop).

Reusable piece already merged: `hinoki_server::Queue` (`push` reports the empty→non-empty edge;
consumer must drain until empty) in `crates/server/src/queue.rs`.

## 2. Recommendation

Replace `bridge!`'s two foreign-thread GIL acquisitions and `call_soon_threadsafe` with a **per-loop
completion queue + one wake fd**, drained on the loop thread:

```
loop thread (GIL held)                          tokio worker (no GIL, ever)
bridge call: fut = loop.create_future()
  id = slab.insert(fut); fut.add_done_callback(cancel(id))
  handle = runtime.spawn(db future)  ─────────► runs the DB call
                                                result: Result<Rust data, DbError>
                                                queue.push((id, Box<dyn FnOnce(Python)->PyResult<Py<PyAny>> + Send>))
                                                if edge: write 1 byte to wake fd
loop.add_reader(fd, drain) ◄──── wake ─────────
drain: clear fd; loop { batch = queue.drain(256); if empty break;
  for (id, convert) in batch: fut = slab.remove(id)?  (None = cancelled, drop result)
        if fut.done(): skip
        else: fut.set_result(convert(py)) or fut.set_exception(err) }
```

* The slab (`id → Py<future>`) lives **only on the loop thread**; the tokio side never touches or
  drops a Python object. Completions carry Rust data and a `Send` conversion closure that holds no
  `Py`. So no GIL is needed on any tokio thread and no `Py` is dropped off-thread.
* Result is set **directly** on the loop thread (no `call_soon_threadsafe`, no `CheckedCompletor`),
  guarded by `fut.done()`.
* One wake per empty→non-empty edge, so a burst of N completions costs one fd write and one
  reader callback instead of N `call_soon_threadsafe` calls.

### Alternatives considered

| Option | Why not |
| --- | --- |
| Keep `future_into_py`, only move the conversion out of the worker (return raw data, convert in a `spawn_blocking`) | Removes step 2 only; keeps the blocking-pool hop and `call_soon_threadsafe`. Cheapest, but does not reach the design goal and still wastes a thread hop per call. Kept as the **fallback** if the queue version regresses. |
| One global (process-wide) queue | Tests and `ASGIClient` create many loops sequentially, some concurrently in threads; a global fd bound to the first loop breaks them. |
| `pyo3-async-runtimes` locals only | Same as today. |
| `eventfd` | Not portable to macOS (the dev host). `UnixStream::pair()` from `std` is safe, non-blocking and works on both. |

## 3. Design details

**Per-loop state.** A thread-local `LoopCompleter { shared: Arc<Shared>, slab: RefCell<HashMap<u64,
Py<PyAny>>>, next_id, loop: Py<PyAny> }`, created at the first bridge call on a thread whose
running loop is not the stored one. `Shared { queue: Queue<Completion> (capacity usize::MAX: a
completion must never be refused, it is already bounded by the callers and the pool), writer:
UnixStream (non-blocking), closed: AtomicBool }`. Thread-local makes "one completer per running
loop" structural (a loop runs on one thread). If the stored loop `is_closed()` at the next call,
the old completer is replaced and its slab dropped with the GIL held.

**Wake fd.** `UnixStream::pair()` both ends non-blocking. Reader end registered with
`loop.add_reader(reader.fileno(), drain)`. The tokio side writes one byte on the `Wake` edge; a
`WouldBlock` is ignored (a wake is already pending). `drain` reads and discards all pending bytes
first, **then** drains the queue until empty: a push racing after the clear re-triggers the edge.
(This is the contract documented on `Queue::drain`.) Drain is capped per batch (256) and yields
back to the loop between batches only if the queue is still non-empty after a full batch
(`loop.call_soon(drain)` rather than looping forever), so one hot burst cannot starve the loop.

**Cancellation.** `fut.add_done_callback(cancel)` (a tiny `#[pyclass]`, like the library's
`PyDoneCallback`): when the asyncio future is cancelled it removes the slab entry and aborts the
tokio task through its `AbortHandle`. Dropping the DB future mid-flight is exactly what the library
does today via `Cancellable`; the ORM's pool/connection drop behaviour is therefore unchanged and
must be re-tested (§6). A completion that arrives for a removed id is dropped in `drain` (Rust data
only).

**Panics.** A guard value inside the spawned task pushes an error completion
(`RustPanic`-equivalent `DatabaseError`) if dropped without having pushed a result and without the
cancel flag set. Abort sets the flag first, so abort is silent.

**Loop without `add_reader`** (Windows proactor, some embedded loops): `add_reader` raises
`NotImplementedError`; the bridge then uses today's `future_into_py` path unchanged. Correctness
never depends on the fast path; the fast path is an optimization.

**Loop closed with operations pending.** The fd stays registered only while the loop lives
(`remove_reader` on replacement). Pending completions of a closed loop are dropped with the
completer (Rust data, no GIL needed). No future is resolved on a closed loop.

**Scope.** In: all `bridge!` call sites in `db.rs` (`connect`, `fetch`, `execute`, `begin`,
`begin_migration`, `Transaction.fetch/execute/commit/rollback`) through one helper replacing the
macro. Out: the request-body awaitable (§3.4 of the parent design): body reading is not wired until
R6; this change only provides the `LoopCompleter` API (`submit(py, fut, convert) -> future`) that R6
will use for it. `PyDatabase`/`PyTransaction` public Python API and exception classes are
unchanged.

**Complexity.** `bridge!` becomes a function `submit` plus a small drain function, each ≤ 15; no
nested functions (owner rule); done-callback is a `#[pyclass]`.

## 4. What this does NOT claim

* It does not make the ORM "GIL-free": rows are still converted to Python on the loop thread
  (before: on a worker holding the GIL anyway). The GIL time per call is the same or slightly
  higher on the loop thread; the gain is fewer foreign-thread GIL handoffs, no blocking-pool hop
  and one wake per burst. Whether that wins is the §7 measurement.
* No effect on request routes without DB access.

## 5. Failure, empty and private states

DB error → `db_error` built on the loop thread (same classes/attributes). Panic → `DatabaseError`.
Cancelled future → no result, no exception, task aborted. Loop closed → nothing resolved, no leak
of the slab (dropped under the GIL). Queue never full. Wake write failure other than `WouldBlock`
(peer closed) → mark `closed`, fall back to resolving through `call_soon_threadsafe` for the
remaining completions (never lose a result). No data/privacy surface changes.

## 6. Tests

Rust (`cargo test`): `LoopCompleter` is mostly Python-bound, so pure parts get unit tests (edge
wake on empty, drain-until-empty, capped batch, guard silent on abort / error on panic).
Python (real SQLite always; PostgreSQL/MySQL when the env var is set):
1. Existing `tests/` DB suites pass unchanged (behaviour parity), on asyncio and uvloop.
2. 2,000 concurrent `fetch` with `gather`: all results correct and complete, no lost wake.
3. Cancel a pending slow statement (PostgreSQL `pg_sleep`): task cancelled, connection returned or
   discarded exactly as before, pool `status()` recovers, no later set on the cancelled future.
4. Many sequential loops (`asyncio.run` ×50) and two loops concurrently in two threads.
5. Loop closed with pending operations: no exception, no hang, no crash at interpreter exit.
6. Error mapping: unique/FK/not-null/check violations keep class and `constraint/table/sqlstate`.
7. Fallback path: loop whose `add_reader` raises `NotImplementedError` (stub loop) still works.
8. Transaction: busy/finished errors and `commit`/`rollback` ordering unchanged.

## 7. Gate (decided before measuring)

A/B against `main` `f672172`, **5 alternating baseline/current runs each**, same extension build
profile (release), same PostgreSQL, same load, record load average and CV:
* Granian ASGI, `/db` route (`benchmarks/comparative/spike_r6` shape or `profile_request.py --prod`)
  with `runtime_threads` 1 and 2, and a `/health` control.
* In-process ORM microbenchmark (fetch of 1 row and 100 rows, 64 concurrent).
Adopt only if **no regression** on Granian beyond the detector policy (15%, 2σ) and the 100-row
case does not lose more than noise (the conversion moves onto the loop). If the fetch-100 case
regresses, try converting in the worker only for large results is NOT allowed (it reintroduces the
GIL on workers); report instead and fall back to the cheap alternative in §2. A failed gate is
reported with the numbers, not forced.

## 8. Build steps and ownership

1. `crates/bindings/src/completion.rs` (new): `Shared`, `Completion`, thread-local `LoopCompleter`,
   `submit`, drain, done-callback, panic guard. Unit tests for the pure parts.
2. `crates/bindings/src/db.rs`: replace `bridge!` call sites by `completion::submit`; keep the
   old path as the `NotImplementedError` fallback. (Serial after 1; same owner; `db.rs` and
   `completion.rs` only. `crates/bindings/Cargo.toml` unchanged: `UnixStream` is `std`.)
3. Python tests in `tests/` (new file, flat name, no underscores) for §6.
4. Docs: `docs/SYNC-ASYNC.md`/ARCHITECTURE note only if user-visible; correct the §3.3 statement in
   `RUST-SERVER-DESIGN.md` (local) with the finding of §1.
5. Verify: cargo fmt/clippy/test, rust-code-analysis gate, ruff, mypy, complexipy, full pytest on a
   disposable PostgreSQL, §7 A/B. Then an independent Opus read-only review (GIL, cancellation of
   pending DB futures, loop close, drain starvation) → fix pass → re-run.
6. Per standing instruction: scoped commits (feat, test, docs), push, PR; merge only with CI green
   and the owner's merge approval already granted.

## 9. Open points (not owner decisions)

* `UnixStream` fileno on Windows is unavailable: Windows uses the fallback path (no `add_reader`
  on proactor anyway). CI runs Linux only; macOS verified locally.
* Batch cap 256 is a guess; tune only with a measured stall, not by taste.
* uvloop's `add_reader` semantics with a `UnixStream` fileno: tested in §6.1 before measuring.
