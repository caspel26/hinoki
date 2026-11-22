# ADR 0005 — Typed events with explicit handler collections

Status: Accepted · 2026-11-23

**Context.** Applications need to react to something that happened (a note saved) without the writer importing the reaction. Django-style signals are global, string-or-sender keyed, untyped and registered by import side effect, so the program flow is invisible. Hinoki already has model lifecycle hooks and `Database.after_commit`; a new mechanism must complete them, not duplicate them.

**Decision.** Events are classes. `hinoki.events.Handlers` collects handlers by event type and phase (`in_transaction`, async only, can roll back; `after_commit`, cannot cancel, failures logged and counted). The application lists its collections in `Hinoki(events=[...])` or `manifest_events()` (an `events = "module:attribute"` key per app in `hinoki.toml`); nothing registers by import. Order is the collection order, then registration order. Each handler has a timeout, handlers per event and phase are bounded, and nested emission has a depth limit. The bus is carried by a `ContextVar`, like the database, so there is no global mutable request state. It is Python: a registration API needs no Rust, and the per-write guard (`EventBus.listening`) is a dictionary lookup.

**Consequences.** Delivery is inside one worker process; there is no queue, no retry and no at-least-once guarantee. Model events (`Created`, `Updated`, `Deleted`) are emitted by the model write path behind the same guard, and a model with handlers for one leaves the native lane for the matching generated action, because the native plan writes without the model; filter UPDATEs and raw SQL emit nothing. A listing command and Prometheus metrics follow in later changes. No `hinoki check` rule is added: a handler body cannot be proven, and registration errors already fail at startup.

**Alternatives.** String-named signals (no typing); a decorator that registers at import (hidden flow); a durable queue (a different feature: jobs).

**Revisit when** a durable or cross-process delivery is requested.
