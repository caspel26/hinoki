# Settings

- `hinoki.settings.Settings() -> None`: Base class: subclasses are keyword-only frozen dataclasses. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.Settings)

- `hinoki.settings.Secret(value: 'str') -> 'None'`: Text that never shows itself: ``repr``, ``str``, ``dump()`` and JSON responses give ``**********``; ``reveal()`` returns the text. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.Secret)

- `hinoki.settings.SettingsError(problems: 'list[str]') -> 'None'`: The configuration is invalid; ``problems`` lists every one (no values). [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.SettingsError)

- `hinoki.settings.AppSettings(...)`: The built-in groups every application has; subclass to add your own. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.AppSettings)

- `hinoki.settings.ExecutionSettings(...)`: ExecutionSettings(*, sync_workers: 'int' = 8, sync_queue: 'int' = 64, request_timeout: 'float' = 30.0, max_body_bytes: 'int' = 1048576, max_query_bytes: 'int'…. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.ExecutionSettings)

- `hinoki.settings.LifecycleSettings(*, startup_timeout: 'float' = 30.0, shutdown_timeout: 'float' = 10.0) -> None`: LifecycleSettings(*, startup_timeout: 'float' = 30.0, shutdown_timeout: 'float' = 10.0). [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.LifecycleSettings)

- `hinoki.settings.LoggingSettings(...)`: LoggingSettings(*, level: "Literal['debug', 'info', 'warning', 'error', 'critical']" = 'info', format: "Literal['text', 'json']" = 'text', include_request_id:…. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.LoggingSettings)

- `hinoki.settings.DocsSettings(...)`: DocsSettings(*, title: 'str' = 'Hinoki API', version: 'str' = '0.1.0', openapi_url: 'str | None' = None, docs_url: 'str | None' = None, redoc_url: 'str |…. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.DocsSettings)

- `hinoki.settings.ServerSettings(...)`: How ``hinoki serve`` and ``dev`` run the application. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.ServerSettings)

- `hinoki.settings.PaginationSettings(*, page_size: 'int' = 20, max_page_size: 'int' = 100, cursor_secret: 'Secret | None' = None, cursor_ttl_seconds: 'int | None' = None) -> None`: Defaults for ``PageNumberPagination`` and ``CursorPagination`` that an action does not set itself. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.PaginationSettings)

- `hinoki.settings.CorsSettings(...)`: Which browser origins may read responses. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.CorsSettings)

- `hinoki.settings.CompressionSettings(...)`: Response compression, done by Kiso only (it is ignored under another runner, and ``hinoki serve --server granian`` refuses ``enabled``). [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.CompressionSettings)

- `hinoki.settings.MetricsSettings(...)`: Prometheus metrics, kept and served by Kiso only (they are ignored under another runner, and ``hinoki serve --server granian`` refuses ``listen``). [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.MetricsSettings)

- `hinoki.settings.EventsSettings(*, handler_timeout: 'float' = 5.0, max_handlers: 'int' = 16, max_depth: 'int' = 4) -> None`: Limits of the typed event handlers (see ``hinoki.events``): the time one handler may take, how many handlers one event and phase may have, and how deep a…. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.EventsSettings)

- `hinoki.settings.SecuritySettings(...)`: Browser response protections and an optional HTTPS redirect. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.SecuritySettings)

- `hinoki.settings.AdminSettings(*, secret: 'Secret | None' = None, session_hours: 'int' = 12, idle_minutes: 'int' = 120, min_password_length: 'int' = 12, theme: 'str | None' = None) -> None`: The admin site: the secret that keys its CSRF tokens and the rules of its own users' sessions and passwords. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.AdminSettings)

- `hinoki.settings.FrameOptions(*values)`: Values accepted by the ``X-Frame-Options`` response policy. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.FrameOptions)

- `hinoki.settings.ReferrerPolicy(*values)`: Values accepted by the ``Referrer-Policy`` response policy. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.ReferrerPolicy)

- `hinoki.settings.DatabaseSettings(...)`: Set either ``url`` or ``engine`` with its connection fields; with neither, the database is local SQLite at ``.hinoki/local.sqlite3``. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.DatabaseSettings)

- `hinoki.settings.DatabaseEngine(*values)`: The backend that ``DatabaseSettings.engine`` selects. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.DatabaseEngine)

- `hinoki.settings.EmailSettings(...)`: Outbound email. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.EmailSettings)

- `hinoki.settings.EmailBackend(*values)`: The delivery ``EmailSettings.backend`` selects. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.EmailBackend)

- `hinoki.settings.EmailSecurity(*values)`: How ``EmailSettings`` secures the SMTP connection. [reference](https://hinoki.caspel.dev/dev/reference/settings/#hinoki.settings.EmailSecurity)
