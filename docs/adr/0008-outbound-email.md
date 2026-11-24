# ADR 0006 — Outbound email from a per-worker mailer

Status: Accepted · 2026-11-23

**Context.** Applications send account and notification email. The framework should own the delivery path (rendering, SMTP, TLS, timeouts, pool, retries) in Rust like the rest of the request path, without a queue or an external email framework, and without sending anything when the project is merely imported or installed.

**Decision.** `hinoki.mail.Mailer` is a per-worker resource, like the database: an `email: EmailSettings` field installs `EmailModule`, which opens the mailer in the worker's lifespan and closes it at shutdown. The `backend` has no default (`smtp`, `console` or `memory`). The `crates/mail` crate renders the message and speaks SMTP over a bounded pool, with certificate verification that cannot be switched off, bounded timeouts and a bounded retry of transient failures only; Python owns the settings, the `Message` type and the context. A send that fails after the end of the message data was written is reported with `possibly_sent` and is never retried. Sending after a transaction goes through `Database.after_commit`. Errors carry a kind and a code but no recipient, subject, body or reply text.

**Consequences.** There is no queue and no delivery guarantee: a crash between a commit and the send loses the message, and applications that cannot accept that keep their own outbox table. There are no SMTPUTF8, XOAUTH2, DKIM, inline images or DSN (templates: amended by ADR 0007). The message and its result cross the binding as Python objects. Metrics are exported by Kiso only; `Mailer.stats()` exists everywhere.

**Alternatives.** The standard library `smtplib` or an async SMTP package in Python (blocking or Python-side protocol code, no shared bounds); a Rust SMTP library behind the binding (a larger dependency surface for a small protocol subset); sending from typed events (a send is an effect to be chosen at the call site, not a reaction); a durable queue (a different feature: jobs).

**Revisit when** a durable queue, an HTTP provider or the admin needs to send email.
