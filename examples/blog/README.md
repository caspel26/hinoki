# Blog

The complete example: everything the other examples leave out, wired as a
production project would be. For outbound email see [Mailroom](../mailroom/README.md). It adds to [Tracker](../tracker/README.md) a
tenant (the publication), password login with JWT sessions, an admin with its own
sign-in and migrations, an application MCP tool, a streamed CSV export, an audit
trail written by event handlers, a rule waiver and optional Prometheus metrics.

## Read it in this order

| # | File | What to see |
| --- | --- | --- |
| 1 | [`hinoki.toml`](hinoki.toml) | The `blog` app (models, migrations, `events`) the admin's own `admin` app with its migrations (the admin is an optional built-in app that needs its tables and an explicit activation) and the `ratelimit` app that owns the shared limiter's table |
| 2 | [`blog/apps/blog/models.py`](blog/apps/blog/models.py) | `Publication` is the tenant; `User`, `Tag` and `Post` each point to one; `Post.tags` is a many-to-many relation limited to 10 tags; `AuditEntry` is written by events |
| 3 | [`blog/apps/blog/schemas.py`](blog/apps/blog/schemas.py) | Request bodies and responses are `Schema` classes, separate from the models; the author and publication are never accepted from a client |
| 4 | [`blog/apps/blog/security.py`](blog/apps/blog/security.py) | `TokenAuth` resolves an opaque bearer token to a `User`; the `editor` and `reader` policies |
| 5 | [`blog/apps/blog/api.py`](blog/apps/blog/api.py) | `@crud` generates create and delete with `owner` and a server-assigned `publication_id`; `links={"tags": tags_of}` scopes the tags a post may carry to the caller's publication, so `tag_ids` on create is checked against them (a tag of another publication is a `422`); `Tags` lists and creates tags; `get_queryset` is the tenant boundary; handwritten `listing`, `drafts`, a streamed CSV `export` and `publish` with `atomic=True`, which emits `PostPublished` |
| 6 | [`blog/apps/blog/events.py`](blog/apps/blog/events.py) | An in-transaction handler audits `Created[Post]` and `PostPublished`; an after-commit handler runs once the data is safe. Declared in `hinoki.toml`, so `hinoki events` lists them |
| 7 | [`blog/apps/blog/accounts.py`](blog/apps/blog/accounts.py) | Password login and `/auth/me` with a JWT; login uses the built-in `RateLimit(20, 60)` per client, counted in a table every worker shares |
| 8 | [`blog/rules.py`](blog/rules.py) | The architecture rules; `require_atomic` exempts only the MCP endpoint |
| 9 | [`hinoki-rules.toml`](hinoki-rules.toml) | A waiver with a reason and an owner: login only reads, so it is not atomic on purpose. `hinoki check` shows it as `[waived]` when `BLOG_JWT_SECRET` is set, and warns when it matches nothing |
| 10 | [`blog/apps/blog/adminsite.py`](blog/apps/blog/adminsite.py) | The admin: model admins, an action, stats and a dashboard (`site` and `PostAdmin`). `routes.py` mounts it at `/admin` |
| 11 | [`blog/main.py`](blog/main.py) | `build_app` assembles the app: routes, rules and the waiver file, the hasher closed at shutdown, and `expose_as_mcp` for `Posts.drafts` |
| 12 | [`blog/settings.py`](blog/settings.py) | The environment variables (`BLOG_*`), including the Kiso-only `BLOG_METRICS__LISTEN` |
| 13 | [`blog/apps/blog/seed.py`](blog/apps/blog/seed.py) | Synthetic demo data for a disposable database |
| 14 | [`blog_tests/`](blog_tests/) | API and tenant, tags (many-to-many), accounts and login limit, rules and waiver, shutdown, audit events, CSV, MCP and admin; [`conftest.py`](blog_tests/conftest.py) migrates a SQLite file per test |

[`blog/apps/blog/migrations/`](blog/apps/blog/migrations/) holds two reviewed SQL
migrations (`0002-tags` adds the tags and the generated bridge table); the admin's own are shipped with Hinoki.

## Run

From this directory. The admin needs a signing secret of at least 32 bytes and,
because its CSRF check compares the `Origin` header with the `Host` header, named
`allowed_hosts`; a served app with the admin and `allowed_hosts` left at `*`
refuses to start (with `BLOG_DEBUG=true` it starts and accepts only loopback
hosts: `localhost`, `127.0.0.0/8`, `[::1]`):

```sh
export BLOG_ADMIN__SECRET=a-local-development-secret-with-32-bytes
export BLOG_ALLOWED_HOSTS=127.0.0.1,localhost
uv run hinoki migrations apply
uv run hinoki admin create-superuser ada
uv run hinoki dev
```

`migrations apply` creates the Blog tables and the admin's user tables in
`.hinoki/local.sqlite3`. For another database export `BLOG_DATABASE__URL`;
`hinoki migrations` and `hinoki dev` both read it. A local SQLite database created before tokens were stored as digests has the old `blog_users` column: delete `.hinoki/local.sqlite3` and migrate again.

Open `/admin` and sign in as the superuser; `/docs` shows the OpenAPI UI.
`BLOG_JWT_SECRET` switches on `POST /auth/login`.

## Demo data

```sh
uv run python -m blog.apps.blog.seed
uv run hinoki migrations apply admin
```

The seed applies the Blog migrations itself (use it instead of
`migrations apply` on a fresh disposable database), then inserts two
publications, three users and twelve posts. It prints one random bearer token
per user, once (the table keeps only its SHA-256 digest): `ada` is the first publication's editor and `bob` the
second's, and each sees only their own drafts. Export `ada`'s token:

```sh
export TOKEN=<the token printed for ada>
curl localhost:8000/posts/drafts -H "authorization: Bearer $TOKEN"
curl localhost:8000/posts/export.csv -H "authorization: Bearer $TOKEN"
```

## Check, MCP and metrics

```sh
uv run hinoki check            # add BLOG_JWT_SECRET=... to see the waiver
uv run hinoki events
uv run hinoki routes
```

These commands build the application, so `BLOG_ADMIN__SECRET` (see Run) must be
exported for them too.

`POST /mcp` serves one read-only tool (`Posts.drafts`) to a caller holding an
editor token. With the Kiso server, `BLOG_METRICS__LISTEN=127.0.0.1:9100` serves
Prometheus metrics on that address, and `uv run hinoki serve --server kiso`
starts it; Granian refuses the setting.

## Test

```sh
uv run pytest examples/blog/blog_tests
```
