# Modules and runtime API

- `hinoki.modules.Module()`: Base class for modules. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.modules.Module)

- `hinoki.modules.ModuleContext(settings: 'object | None' = None) -> 'None'`: Per-worker context handed to module lifespans. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.modules.ModuleContext)

- `hinoki.management.Command()`: A project command with explicit arguments and module requirements. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.management.Command)

- `hinoki.management.CommandContext(settings: 'object | None', modules: 'ModuleContext') -> 'None'`: Settings and resources available during one command invocation. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.management.CommandContext)

- `hinoki.management.CommandError`: The command could not start or returned an invalid result. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.management.CommandError)

- `hinoki.runtime.config.RuntimeConfig(...)`: RuntimeConfig(sync_workers: 'int' = 8, sync_queue: 'int' = 64, request_timeout: 'float' = 30.0, startup_timeout: 'float' = 30.0, shutdown_timeout: 'float' =…. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.runtime.config.RuntimeConfig)

- `hinoki.errors.ModuleGraphError`: Missing dependency, dependency cycle or duplicate module name. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.errors.ModuleGraphError)

- `hinoki.errors.RegistrationError`: Invalid or conflicting declarations, raised before the app serves. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.errors.RegistrationError)

## Events

The [events guide](https://hinoki.caspel.dev/dev/events.md) explains the two moments a handler runs in.
A handler collection is listed on the application, or in an app's `events`
entry of `hinoki.toml`.

- `hinoki.events.Handlers(name: 'str' = '') -> 'None'`: A collection of event handlers, listed on the application to take effect. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.Handlers)

- `hinoki.events.Created(subject: 'type[M]', instance: 'M') -> None`: A model row was inserted by ``insert`` (``acreate``, ``asave`` of a new instance, ``abulk_create``, generated CRUD create). [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.Created)

- `hinoki.events.Updated(subject: 'type[M]', instance: 'M', changed: 'tuple[str, ...]') -> None`: A model instance was saved with changes (``asave``, ``aupdate_from``, ``abulk_update``, generated CRUD update). [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.Updated)

- `hinoki.events.Deleted(subject: 'type[M]', count: 'int') -> None`: A DELETE statement removed ``count`` rows of the model (``adelete``, ``abulk_delete``, generated CRUD delete). [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.Deleted)

- `hinoki.events.emit(event: 'object') -> 'None'`: Emit ``event`` on the current worker's bus. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.emit)

- `hinoki.events.listener(kind: 'type', subject: 'type | None' = None) -> 'EventBus | None'`: The current bus when a write of ``subject`` (or, for an event with no subject, any emitter) must emit ``kind``, else ``None``: the guard of the emitting code…. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.listener)

- `hinoki.events.Linked(subject: 'type[M]', relation: 'str', key: 'object', targets: 'tuple[object, ...]') -> None`: Links were added to a many-to-many relation (``add`` or ``set`` of a ``LinkSet``). [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.Linked)

- `hinoki.events.Unlinked(subject: 'type[M]', relation: 'str', key: 'object', targets: 'tuple[object, ...]') -> None`: Links were removed from a many-to-many relation (``remove`` or ``set`` of a ``LinkSet``); the fields are those of ``Linked``. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.Unlinked)

- `hinoki.events.AuthRejected(reason: 'str', route: 'str', method: 'str') -> None`: A route's access policy refused a request (HTTP 401 or 403). [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.AuthRejected)

- `hinoki.events.RateLimited(policy: 'str', route: 'str', method: 'str', retry_after: 'int') -> None`: A route's rate-limit policy refused a request (HTTP 429). [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.events.RateLimited)

- `hinoki.wsgi.WSGIAdapter(app: 'Any') -> 'None'`. [reference](https://hinoki.caspel.dev/dev/reference/runtime/#hinoki.wsgi.WSGIAdapter)
