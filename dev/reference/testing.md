# Testing

- `hinoki.testing.ASGIClient(app: 'Hinoki', strict_errors: 'bool' = False, *, host: 'str | None' = None) -> 'None'`: Use as an async context manager so startup and shutdown always run. [reference](https://hinoki.caspel.dev/dev/reference/testing/#hinoki.testing.ASGIClient)

- `hinoki.testing.SyncClient(app: 'Hinoki', timeout: 'float' = 60.0, *, host: 'str | None' = None) -> 'None'`: The same client for tests written without ``async``: the application runs on its own event loop in a background thread and every call blocks until the answer…. [reference](https://hinoki.caspel.dev/dev/reference/testing/#hinoki.testing.SyncClient)

- `hinoki.testing.ManualClock(start: 'datetime') -> 'None'`: A thread-safe, controllable UTC clock for deterministic tests. [reference](https://hinoki.caspel.dev/dev/reference/testing/#hinoki.testing.ManualClock)
