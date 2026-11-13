# Running an application

```sh
hinoki dev   blog.main:create_app     # development: reload on change
hinoki serve blog.main:create_app     # production-style: no reload
```

`TARGET` is `module:name`, where `name` is an application (`Hinoki`) or a function that returns one (a factory: the usual choice, because it builds the app inside each worker). The factory builds declarations only; databases and pools open in the worker's lifespan.

Both commands run the app under Granian, so install the server extra: `pip install 'hinoki[server]'` (the generated project already depends on it).

## Where host, port and workers come from

From the application's **`server` settings group** ([settings guide](SETTINGS.md)); flags override them:

| Setting | Variable (prefix `BLOG_`) | Default | Flag |
| --- | --- | --- | --- |
| `server.host` | `BLOG_SERVER__HOST` | `127.0.0.1` | `--host` |
| `server.port` | `BLOG_SERVER__PORT` | `8000` | `--port` |
| `server.workers` | `BLOG_SERVER__WORKERS` | `1` | `--workers` (serve) |
| `server.runtime_threads` | `BLOG_SERVER__RUNTIME_THREADS` | `1` | |
| `server.backlog` | `BLOG_SERVER__BACKLOG` | `1024` | |
| `server.access_log` | `BLOG_SERVER__ACCESS_LOG` | `false` | |

`dev` always runs one worker with reload. The settings command shows what is in effect: `hinoki settings blog.settings:BlogSettings`.

## Declaring where settings are read

The settings class says its own prefix and `.env` file, so nothing is repeated at the call site:

```python
class BlogSettings(FrameworkSettings, prefix="BLOG_", dotenv=".env"):
    database_url: Secret

settings = BlogSettings.load()          # reads BLOG_* and .env
```

## Other runners

`dev` and `serve` are conveniences. The application is a plain ASGI app, so `granian --interface asgi --factory blog.main:create_app` (or another ASGI server) works too; then the `server` group is ignored, because only `hinoki serve` applies it.
