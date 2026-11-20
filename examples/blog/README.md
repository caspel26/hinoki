# Blog

The reference app: typed settings, bearer authentication, generated CRUD
routes, handwritten publication actions, explicit relation loading,
transactions, startup rules and the optional admin. `main.py` holds the
factory; `accounts.py` adds password login only when a JWT secret is set.

## Run

From `examples/`. The admin needs a signing secret:

```sh
export BLOG_ADMIN__SECRET=a-local-development-secret-with-32-bytes
uv run hinoki migrations apply
uv run hinoki admin create-superuser ada
uv run hinoki dev blog.main:create_app
```

`migrations apply` creates the Blog tables and the admin's user tables, in
dependency order. The checked-in SQL is in [`migrations/`](migrations/).
The database is `.hinoki/local.sqlite3`; for another one, set
`BLOG_DATABASE__URL` and `HINOKI_DATABASE_URL` to the same URL before applying.

## Sign in

Open `/admin` and sign in as the superuser; add editors in its Users section and
tick what each may do. Open `/docs` for the OpenAPI UI. `BLOG_JWT_SECRET`
enables `POST /auth/login`, which is rate limited by `limits.LoginAttempts`
(a fixed window per client address, in one worker's memory; see
[rate limit an action](../../docs/endpoints.md#rate-limit-an-action)).

## Demo data

```sh
uv run python -m blog.seed
```

The seed applies the Blog migrations and inserts fixed synthetic users and
posts; the editor's bearer token is `demo-editor-token`. Run it once, against a
fresh disposable database: the demo records are unique and not a resettable
fixture. Never use real credentials or user data with it.

## Rules

All eleven built-in rules are on in `rules.py`
([rules](../../docs/rules.md)): `hinoki check blog.main:create_app` exits 0, and
a worker refuses to start while one is violated.
