# Admin API

- `hinoki.admin.Admin(...)`: The admin site: prefix, access policy, theme and registered models. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Admin)

- `hinoki.admin.ModelAdmin() -> 'None'`: How one model appears in the admin. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.ModelAdmin)

- `hinoki.admin.BulkAction(name: 'str', label: 'str', confirm: 'str', handler: 'Callable[[tuple[M, ...], Caller[Any]], Awaitable[None]]') -> None`: A confirmed action for selected rows in a model's admin list. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.BulkAction)

- `hinoki.admin.Theme(...)`: Theme(name: 'str', label: 'str', edit: 'str', light: 'Mapping[str, str]', dark: 'Mapping[str, str]', fixed: 'Mapping[str, str]', default_mode: 'str' =…. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Theme)

- `hinoki.admin.Avatar(name: 'str') -> None`: A person: initials in a circle plus the name. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Avatar)

- `hinoki.admin.Badge(label: 'str', tone: 'Tone | str' = <Tone.NEUTRAL: 'neutral'>, icon: 'str' = 'circle-check') -> None`: A status pill. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Badge)

- `hinoki.admin.Tab(label: 'str', filters: 'Mapping[str, Any]' = <factory>) -> None`: A saved filter shown as a tab: ``Tab("Drafts", {"published": False})``. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Tab)

- `hinoki.admin.Fieldset(title: 'str', fields: 'tuple[str, ...]', collapsed: 'bool' = False) -> None`: A titled group of form fields; ``collapsed`` renders it closed. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Fieldset)

- `hinoki.admin.Extra(name: 'str', label: 'str', kind: 'str', note: 'str' = '', choices: 'tuple[tuple[str, str], ...]' = ()) -> None`: A form field that is not a column of the model. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Extra)

- `hinoki.admin.Refusal`: A write the admin refuses with a message meant for the person: raised from ``save`` or ``remove``, it is shown on the form and rolls the write back. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Refusal)

- `hinoki.admin.Event(text: 'str', when: 'str', icon: 'str' = 'pencil', tone: 'Tone | str' = <Tone.ACCENT: 'accent'>) -> None`: One line of the recent-activity panel. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Event)

- `hinoki.admin.Link(label: 'str', href: 'str', icon: 'str' = 'settings') -> None`: An extra sidebar link (settings, activity log, ...) supplied by the app. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Link)

- `hinoki.admin.Stat(label: 'str', value: 'int | str', icon: 'str' = 'file-text', tone: 'Tone | str' = <Tone.ACCENT: 'accent'>, series: 'tuple[float, ...]' = ()) -> None`: A dashboard card. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Stat)

- `hinoki.admin.THEMES`: dict() -> new empty dictionary dict(mapping) -> new dictionary initialized from a mapping object's (key, value) pairs dict(iterable) -> new dictionary…. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.THEMES)

- `hinoki.admin.action(label: 'str' = '', *, confirm: 'str' = '') -> 'Callable[[F], F]'`: Declare a bulk action on a ``ModelAdmin`` method. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.action)

- `hinoki.admin.Tone(*values)`: The color role of a badge, stat or event. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.Tone)

- `hinoki.admin.users.limits.LoginWindow(limit: 'int' = 10, window: 'int' = 60, clock: 'Callable[[], float]' = monotonic) -> 'None'`: At most ``limit`` attempts per ``window`` seconds from one address. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.users.limits.LoginWindow)

- `hinoki.admin.users.limits.DatabaseLoginWindow(limit: 'int' = 10, window: 'int' = 60, clock: 'Callable[[], datetime]' = utc_now, timeout: 'float' = 0.5) -> 'None'`: At most ``limit`` attempts per ``window`` seconds from one address on one route, counted by the shared ``RateLimit`` in the ``hinoki_admin_rate_limits``…. [reference](https://hinoki.caspel.dev/dev/reference/admin/#hinoki.admin.users.limits.DatabaseLoginWindow)
