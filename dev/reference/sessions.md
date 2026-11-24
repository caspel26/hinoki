# Sessions

`from hinoki.sessions import SessionModule, SessionAuth, Session`. The [sessions guide](https://hinoki.caspel.dev/dev/sessions.md) explains configuration, cookies and CSRF.

- `hinoki.sessions.SessionModule(store: 'SessionStore', settings: 'SessionSettings') -> 'None'`: Provides a worker-owned session store and session policy. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.SessionModule)

- `hinoki.sessions.SessionSettings(...)`: SessionSettings(cookie_name: 'str' = 'hinoki_session', cookie_secure: 'bool' = True, max_bytes: 'int' = 16384, absolute_timeout: 'timedelta' =…. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.SessionSettings)

- `hinoki.sessions.Session(...)`: Mutable state scoped to one request and persisted only after success. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.Session)

- `hinoki.sessions.SessionAuth(resolve: 'Callable[[Mapping[str, object]], Caller[T] | Awaitable[Caller[T] | None] | None]', *, cookie_name: 'str' = 'hinoki_session')`: Resolve an authenticated caller from the active request's session data. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.SessionAuth)

- `hinoki.sessions.SessionStore()`: Worker-owned store contract; records are keyed by token digest. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.SessionStore)

- `hinoki.sessions.MemorySessionStore(max_sessions: 'int' = 10000) -> 'None'`: Bounded, process-local store for tests and single-worker deployments. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.MemorySessionStore)

- `hinoki.sessions.DatabaseSessionStore(database: 'Database | None' = None) -> 'None'`: Shared store using an explicitly migrated ``hinoki_sessions`` table. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.DatabaseSessionStore)

- `hinoki.sessions.SessionRecord(...)`: SessionRecord(data: 'dict[str, object]', created_at: 'datetime', expires_at: 'datetime', idle_expires_at: 'datetime', last_seen_at: 'datetime', subject: 'str…. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.SessionRecord)

- `hinoki.sessions.SessionError(...)`: Base failure for session persistence and decoding. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.SessionError)

- `hinoki.sessions.SessionUnavailable(...)`: The configured session store could not serve the request. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.SessionUnavailable)

- `hinoki.sessions.SessionTooLarge(...)`: A serialized session exceeds its configured size limit. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.SessionTooLarge)

- `hinoki.sessions.SessionCapacityExceeded(...)`: A memory session backend reached its configured number of live sessions. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.SessionCapacityExceeded)

- `hinoki.sessions.migrations_path() -> 'Path'`: Path to the explicit application-session migration files. [reference](https://hinoki.caspel.dev/dev/reference/sessions/#hinoki.sessions.migrations_path)
