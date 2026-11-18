# Blog

The reference app combines typed settings, bearer authentication, generated
CRUD routes, handwritten publication actions, explicit relation loading,
transactions, startup rules and the optional admin. `main.py` assembles the
factory; `accounts.py` adds password login only when a JWT secret is configured.

## Run locally

The Blog requires an admin signing secret. Its database defaults to local
SQLite:

```sh
cd examples
export BLOG_ADMIN_SECRET='a-local-development-secret-with-32-bytes'
uv run --project .. hinoki app list
uv run --project .. hinoki migrations status
uv run --project .. hinoki migrations plan --app blog --dry-run
uv run --project .. hinoki migrations sql --app blog 0001-initial
uv run --project .. hinoki migrations sql --app blog 0002-accounts
uv run --project .. hinoki migrations apply --app blog
uv run --project .. hinoki dev blog.main:create_app
```

Open `/docs` for OpenAPI or `/admin` for the editor interface. `BLOG_JWT_SECRET`
optionally enables `/auth/login`; without it, use the seeded editor token only
for local exploration. To select a server database, set
`BLOG_DATABASE__URL` and `HINOKI_DATABASE_URL` to the same URL before applying
migrations and starting the app.

For demo records, `BLOG_DATABASE__URL=... uv run --project .. python -m blog.seed`
applies migrations and inserts fixed synthetic users and posts. Run it only
once against a fresh disposable database: those unique demo records are not a
resettable fixture. Never use real credentials or user data with this command.

The manifest registers Notes, Tracker and Blog as separate apps, each with its
own models and migrations. `hinoki migrations apply` initializes all three in
dependency order.
