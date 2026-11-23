---
status: implemented
related:
  - settings.md
  - modules.md
  - transactions.md
  - metrics.md
  - testing.md
---

# Send email

Hinoki sends outbound email over SMTP from a per-worker `Mailer`. The message is rendered, the SMTP conversation, TLS, the connection pool, timeouts and retries all run in Rust (`crates/mail`); the Python side is the `Message` you build, the settings and the module that owns the mailer. The call crosses the Python/Rust boundary once per message, with Python objects for the message and its result.

What it is **not**: there is no queue, no delivery guarantee, no template engine and no HTTP provider (the SMTP and the two local backends below are all there is). `send` returns when the server accepted the message, or raises. The [admin](admin.md) sends no email in 0.1.0.

## Set it up

Declare an `email: settings.EmailSettings` field. Hinoki installs `EmailModule` for you, like the database module, and opens one `Mailer` in every worker's lifespan.

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(settings.AppSettings, prefix="NOTES_"):
    email: settings.EmailSettings
```

`backend` is required and has no default, so nothing is sent by accident. In development use `console` (the message is printed) and in tests `memory`:

```sh
NOTES_EMAIL__BACKEND=console
NOTES_EMAIL__DEFAULT_SENDER=noreply@example.com
```

For production SMTP:

```sh
NOTES_EMAIL__BACKEND=smtp
NOTES_EMAIL__HOST=smtp.example.com
NOTES_EMAIL__USERNAME=mailer
NOTES_EMAIL__PASSWORD_FILE=/run/secrets/smtp-password
NOTES_EMAIL__DEFAULT_SENDER=noreply@example.com
```

Importing Hinoki or the application opens no connection: the mailer is created when the worker starts and closed (`aclose`) when it stops. Each worker has its own pool; nothing is shared between workers or event loops.

## Send a message

```python
# notes/routes.py
from hinoki import http, mail


@http.post("/welcome")
async def welcome() -> dict[str, str]:
    sent = await mail.send(
        mail.Message(
            to=("ada@example.com",),
            subject="Welcome",
            text="Your account is ready.",
            html="<p>Your account is ready.</p>",
        )
    )
    return {"message_id": sent.message_id}
```

`mail.send` uses the mailer of the running worker. It returns a `SentMail` (`message_id`, the envelope `recipients`, `attempts`) and raises `EmailError` on failure.

A `Message` is frozen and keyword-only. `to`, `cc`, `bcc` and `reply_to` are tuples; passing one string where a tuple is expected is a `TypeError`. At least one of `text` or `html` is required (both make a `multipart/alternative`), and at least one recipient in `to`, `cc` or `bcc`. `sender` defaults to `default_sender`; with neither set, `send` raises `invalid_message` for `sender`.

- An address given as `str` is a bare `user@example.com`. To add a display name use `mail.Address("ada@example.com", "Ada Lovelace")`; `"Ada <ada@example.com>"` as a `str` is refused.
- `bcc` recipients are in the SMTP envelope and never in a header.
- `headers=(("X-Campaign", "welcome"),)` adds headers. Names and values are checked: a line break or any other control character (below U+0020 except a tab, or U+007F) in a header value, the subject or a display name is `invalid_header`, and the headers Hinoki writes itself (`From`, `To`, `Date`, `Message-ID`, `Subject`, ...) cannot be set. Text that contains `=?` is always written as encoded words so a reader cannot mistake it for one.
- The sizes are measured before anything is copied or encoded: text, HTML and attachments over `max_message_bytes` are `limit_exceeded` for `text`, `html` or `attachments`.
- A message refused in Python (no sender, a malformed header or attachment) counts as a failed send in `mailer.stats()` and in the metrics, like any other refusal. On a closed mailer every send is `closed`.
- `attachments=(mail.Attachment("report.pdf", data, "application/pdf"),)`. A non-ASCII file name is sent with an RFC 2231 `filename*` parameter.
- Header-injection attempts (a `\r` or `\n` in the subject, a display name or an address) are refused as `invalid_header` or `invalid_address` before anything is sent.

SMTPUTF8 (a non-ASCII address), XOAUTH2, DKIM signing, inline images, DSN requests and PIPELINING are not supported.

## Send after a commit

Send once the data is durable, never from inside a transaction that can still roll back. Register the send with `after_commit`: it runs after the outermost commit and is dropped on rollback.

```python
# notes/apps/accounts/operations.py
from hinoki import db, mail
from hinoki.orm import current_database


async def invite(address: str) -> None:
    async with db.atomic():
        await Invite.acreate(InviteIn(email=address))
        current_database().after_commit(
            lambda: mail.send(
                mail.Message(to=(address,), subject="You are invited", text="Join us.")
            )
        )
```

A send that fails after the commit is logged and does not undo the commit; the callback is not retried. See [after-commit callbacks](transactions.md#after-commit-callbacks).

### When the message must not be lost

Hinoki keeps no queue and cannot promise delivery: a crash after the commit and before the send loses the message. When that matters, write the message into your own table in the same transaction (an outbox) and send it from a [management command](modules.md) that marks each row sent. That table, its retry policy and its cleanup are yours.

## Send from a synchronous handler

A sync handler runs on a thread. Run the send on the worker's loop with `run_sync`:

```python
from hinoki import http, mail
from hinoki.blocking import run_sync


@http.post("/notify")
def notify() -> dict[str, str]:
    sent = run_sync(
        mail.send(mail.Message(to=("ada@example.com",), subject="Hi", text="Hello"))
    )
    return {"message_id": sent.message_id}
```

The mailer is also available in `on_startup` hooks, [management commands](modules.md) that require `EmailModule`, and `hinoki shell`.

## Backends

| `backend` | What it does |
| --- | --- |
| `smtp` | Delivers to `host:port` over a pooled, bounded set of connections |
| `console` | Writes the rendered message to standard output, followed by a line of 72 dashes. Nothing leaves the process |
| `memory` | Renders the message and keeps it in `mailer.outbox` (a bounded deque, `memory_capacity` messages); nothing leaves the process |

`console` and `memory` validate and render exactly as `smtp` does, so a message that is refused in one is refused in all, and `SentMail.raw` holds the bytes that would be sent.

## SMTP, TLS and authentication

| `security` | Default port | Meaning |
| --- | --- | --- |
| `starttls` (default) | 587 | Plain connect, then upgrade. The upgrade is required: a server that does not offer `STARTTLS` is a `tls` error |
| `tls` | 465 | TLS from the first byte |
| `none` | 25 | No encryption. On a host off the loopback it needs `allow_insecure = true` |

- The server certificate and host name are always verified, against the public roots bundled with Hinoki plus the certificates of `tls_ca_file` (a PEM file) when set: the file adds trust, it does not replace the bundled roots, and the operating system's store is not read. There is no setting that turns verification off. An IPv6 `host` may be written `::1` or `[::1]`.
- `username` and `password` are set together. `password` is a [`Secret`](settings.md): it is read from the environment or `PASSWORD_FILE`, never appears in a `repr`, a log line or an exception, and is revealed only to hand it to the SMTP login. Authentication tries `PLAIN`, then `LOGIN`, and a password is never sent in clear text to a non-loopback peer unless `allow_insecure` is set.
- `check_on_startup = true` connects and authenticates while the worker starts and aborts the start on failure; `await mailer.check()` does the same on demand without sending a message. Both are off by default.

## Settings

All fields are in the `email` group. Durations are seconds, more than 0 and at most 3600 (one hour).

| Field | Default | Meaning |
| --- | --- | --- |
| `backend` | required | `smtp`, `console` or `memory` |
| `default_sender` | unset | The `From` address when a message has none |
| `host`, `port`, `security` | `localhost`, by `security`, `starttls` | The server |
| `username`, `password` | unset | Both or neither |
| `allow_insecure` | `false` | Permit `security=none` off the loopback, and plain `AUTH` there |
| `tls_ca_file` | unset (bundled public roots only) | PEM file of certificates trusted in addition to the bundled roots |
| `helo_name` | the local address as a literal (`[192.0.2.1]`) | Name sent in `EHLO`: a domain name, `localhost` or an address literal (`[192.0.2.1]`, `[IPv6:::1]`) |
| `message_id_domain` | domain of `default_sender` (an internationalized domain in its punycode form), else `helo_name`, else `localhost` | Right-hand side of the `Message-ID`; same forms as `helo_name` |
| `connect_timeout`, `command_timeout`, `data_timeout` | `10`, `30`, `120` | Per connect, per command and per message body |
| `timeout` | `60` | The whole `send`, including the wait for a connection and the retries; at least `connect_timeout` |
| `max_connections` | `4` | Connections per worker |
| `pool_wait` | `5` | How long a send waits for a free connection before a `busy` error |
| `idle_timeout` | `60` | Idle connections older than this are closed |
| `max_messages_per_connection` | `100` | A connection is replaced after this many messages |
| `retries`, `backoff_base`, `backoff_max` | `2`, `0.5`, `5` | See [retries](#errors-and-retries) |
| `max_recipients`, `max_message_bytes`, `max_attachments` | `100`, 10 MiB, `20` | Limits checked before connecting |
| `memory_capacity` | `1000` | Size of the `memory` outbox |
| `check_on_startup` | `false` | See above |

A value out of range, a duration over one hour, an empty `username`, `username` without `password`, a `host` longer than 253 characters or with a control character, a `default_sender`, `helo_name` or `message_id_domain` that is not valid, or `security=none` to a remote host without `allow_insecure` is refused when the settings load, with the variable named and the value never shown. `tls_ca_file` is read when the worker starts, not when the settings load.

## Errors and retries

Every failure is an `EmailError` with a `kind` (`EmailErrorKind`):

| `kind` | Cause |
| --- | --- |
| `invalid_address`, `invalid_header`, `invalid_message` | The message is refused before any connection. `field` names the part (`to`, `subject`, `attachments`, ...) and `index` the entry |
| `limit_exceeded` | `max_recipients`, `max_message_bytes` or `max_attachments` |
| `closed` | The mailer was closed (the worker is stopping) |
| `busy` | No connection became free within `pool_wait` |
| `connect`, `timeout`, `tls`, `auth` | The connection, a timeout, the TLS handshake or the login failed |
| `rejected` | The server refused with a permanent `5xx` reply; `code` and `enhanced_status` hold it |
| `temporary` | A `4xx` reply that survived the retries |
| `protocol` | The server broke the SMTP protocol |

The exception text is a fixed sentence plus the field and reply code. It never contains a recipient, a subject, a body, a password or the server's reply text, so it is safe to log.

Connection failures (`connect`), timeouts, `4xx` replies and protocol errors are retried up to `retries` times, all inside `timeout`. The wait before a retry doubles from `backoff_base` up to `backoff_max` and is then scaled by a random factor between one half and one. `attempts` counts the tries. A permanent `5xx` reply, a TLS or authentication failure, a validation error, a full pool and a closed mailer are never retried.

`possibly_sent` is `True` when the connection failed or stalled after the end of the message data was written: the server may have accepted the message. Nothing is retried then, because a retry could send a duplicate; decide in your code whether to send again.

## Test with the memory backend

```python
# tests/test_invite.py
from hinoki import Hinoki, mail, settings
from hinoki.testing import ASGIClient


class TestSettings(settings.AppSettings, prefix="NOTES_"):
    email: settings.EmailSettings


async def test_welcome_mail() -> None:
    app = Hinoki(
        settings=TestSettings(
            email=settings.EmailSettings(
                backend=settings.EmailBackend.MEMORY,
                default_sender="noreply@example.com",
            )
        ),
        include=[welcome],
    )
    async with ASGIClient(app) as client:
        await client.post("/welcome")
        [sent] = next(
            m for m in app.modules if isinstance(m, mail.EmailModule)
        ).mailer.outbox
        assert sent.message is not None and sent.message.subject == "Welcome"
        assert b"Your account is ready." in (sent.raw or b"")
```

`outbox` holds `SentMail` entries with `message` and `raw`; `clear_outbox()` empties it. Each started application, and each worker, has its own outbox. For SMTP behaviour, point `host` at a fake server on `127.0.0.1`; never send real mail from a test.

## Metrics

With `metrics.listen` on [Kiso](kiso.md) and an email module installed, three families are exported. The series are fixed at startup, so no address, subject or error text becomes a label. See [export Prometheus metrics](metrics.md#email).

`Mailer.stats()` is always available, under Granian too: it returns the counts of sent and failed messages, retries, and open and idle connections of that worker.

## What email does not do

- No queue, no scheduled sends and no guaranteed delivery; see [when the message must not be lost](#when-the-message-must-not-be-lost).
- No templates: build the `text` and `html` yourself.
- No HTTP email providers (only SMTP, console and memory).
- No bounce or delivery tracking, no DKIM signing, no SMTPUTF8, no XOAUTH2.
- The admin sends no email in 0.1.0.
- No sharing of a mailer between workers, processes or event loops.
