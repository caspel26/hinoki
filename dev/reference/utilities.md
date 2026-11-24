# Utilities

- `hinoki.utils.Clock(*args, **kwargs)`: A callable clock; inject one when time-dependent behavior needs control. [reference](https://hinoki.caspel.dev/dev/reference/utilities/#hinoki.utils.Clock)

- `hinoki.utils.utc_now() -> 'datetime'`: Return the current time as a timezone-aware UTC datetime. [reference](https://hinoki.caspel.dev/dev/reference/utilities/#hinoki.utils.utc_now)

- `hinoki.utils.utc_today(clock: 'Clock' = utc_now) -> 'date'`: Return the UTC calendar date from an aware clock value. [reference](https://hinoki.caspel.dev/dev/reference/utilities/#hinoki.utils.utc_today)

- `hinoki.orm.timestamps(updated_at: 'str' = 'updated_at', *, clock: 'Clock' = utc_now) -> 'Callable[[M], M]'`: Update a declared datetime column on each ORM update statement. [reference](https://hinoki.caspel.dev/dev/reference/utilities/#hinoki.orm.timestamps)
