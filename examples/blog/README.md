# Blog

The reference app: typed settings, bearer authentication, generated CRUD
routes, handwritten publication actions, explicit relation loading,
transactions, startup rules and the optional admin. `main.py` declares the
application from the shared list in `routes.py`; the project package holds only
that wiring, the settings and the rules, and the `apps/blog/` app owns the models,
schemas, endpoints and migrations. `apps/blog/accounts.py` adds password
login only when a JWT secret is set.

## Run

From `examples/`. The admin needs a signing secret:

```sh
export BLOG_ADMIN__SECRET=a-local-development-secret-with-32-bytes
uv run hinoki migrations apply
uv run hinoki admin create-superuser ada
uv run hinoki dev blog.main:app
```

`migrations apply` creates the Blog tables and the admin's user tables, in
dependency order. The checked-in SQL is in [`apps/blog/migrations/`](apps/blog/migrations/).
The database is `.hinoki/local.sqlite3`; for another one, set
`BLOG_DATABASE__URL` and `EXAMPLES_DATABASE__URL` (read by the migration
commands) to the same URL before applying.

## Sign in

Open `/admin` and sign in as the superuser; add editors in its Users section and
tick what each may do. The admin's theme (`studio`, `console`, `paper` or `bento`)
is set where the site is built, in `apps/blog/adminsite.py`. Open `/docs` for the OpenAPI
UI. `BLOG_JWT_SECRET`
enables `POST /auth/login`, which is rate limited by `limits.LoginAttempts`
(a fixed window per client address, in one worker's memory; see
[rate limit an action](../../docs/endpoints.md#rate-limit-an-action)).

## Demo data

```sh
uv run python -m blog.apps.blog.seed
```

The seed applies the Blog migrations itself and inserts fixed synthetic users
and posts; the editor's bearer token is `demo-editor-token`. Run it once,
against a fresh disposable database instead of `migrations apply` (it fails on
tables that already exist), then `uv run hinoki migrations apply admin` for the
admin's tables. The demo records are unique and not a resettable fixture. Never
use real credentials or user data with it.

## Rules

All eleven built-in rules are on in `rules.py`
([rules](../../docs/rules.md)): `hinoki check blog.main:app` exits 0, and
a worker refuses to start while one is violated.
