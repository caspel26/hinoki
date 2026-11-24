# Mailroom

A focused example: email templates. Two TOML template files with typed contexts,
one template built from strings, routes that send them behind an API key, and a
preview from the command line. There is no database, no user sign-in and no admin.

## Read it in this order

| # | File | What to see |
| --- | --- | --- |
| 1 | [`mailroom/messages.py`](mailroom/messages.py) | The context `Schema` of each message and the two `Template.file(...)` values |
| 2 | [`mailroom/templates/welcome.toml`](mailroom/templates/welcome.toml) | A template file: `subject`, `template` (HTML), `{% if %}`, `{% for %}` and `{{ path }}` |
| 3 | [`mailroom/templates/reset_password.toml`](mailroom/templates/reset_password.toml) | A second file, whose plain text is derived from the HTML |
| 4 | [`mailroom/routes.py`](mailroom/routes.py) | Routes that require an API key (`Access`), bound every request field like the context, build the reset link on the server from `public_base_url` and a fresh `random_token()`, render with `Template.message(...)` and `mail.send(...)` |
| 5 | [`mailroom/notices.py`](mailroom/notices.py) | A template from strings with a `dict` context: only its syntax is checked early |
| 6 | [`mailroom/main.py`](mailroom/main.py) | `Hinoki(email_templates=[...])` registers the templates; files are read when a worker starts |
| 7 | [`mailroom/rules.py`](mailroom/rules.py) | Listed in `hinoki.toml`: every route names an access policy, request strings are bounded and `EmailTemplatesMatch()` checks the templates |
| 8 | [`mailroom_tests/`](mailroom_tests) | Sends through the `memory` backend, checks the key and the bounds, and previews through the CLI |

## Run

From this directory (`examples/mailroom`). The email backend is required;
`console` prints each message and `memory` keeps it:

```sh
export MAILROOM_EMAIL__BACKEND=console
export MAILROOM_EMAIL__DEFAULT_SENDER=noreply@example.com
export MAILROOM_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MAILROOM_PUBLIC_BASE_URL=https://app.example.com  # where reset links point
uv run hinoki check --allow-effects
uv run hinoki mail check
uv run hinoki mail preview welcome
uv run hinoki mail preview reset_password --context mailroom/samples/reset_password.json
uv run hinoki dev
```

In another terminal:

```sh
curl -X POST http://127.0.0.1:8000/signup -H "x-api-key: $MAILROOM_API_KEY" -H 'content-type: application/json' -d '{"email":"ada@example.com","name":"Ada"}'
```

The `POST` answers `202 Accepted` and the message is printed by the server
(`console`); nothing is sent over the network. Use only synthetic addresses such
as `example.com`.

The tests run with the repository's `uv run pytest`, or alone with
`uv run pytest examples/mailroom/mailroom_tests`. The full guide is
[Email templates](../../docs/email-templates.md).
