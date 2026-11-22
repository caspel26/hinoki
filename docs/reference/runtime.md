# Modules and runtime API

::: hinoki.modules.Module
    options:
      show_if_no_docstring: true

::: hinoki.modules.ModuleContext
    options:
      show_if_no_docstring: true

::: hinoki.management.Command
    options:
      show_if_no_docstring: true

::: hinoki.management.CommandContext
    options:
      show_if_no_docstring: true

::: hinoki.management.CommandError

::: hinoki.runtime.config.RuntimeConfig

::: hinoki.errors.ModuleGraphError

::: hinoki.errors.RegistrationError

## Events

The [events guide](../events.md) explains the two moments a handler runs in.
A handler collection is listed on the application, or in an app's `events`
entry of `hinoki.toml`.

::: hinoki.events.Handlers
    options:
      show_if_no_docstring: true

::: hinoki.events.Created

::: hinoki.events.Updated

::: hinoki.events.Deleted

::: hinoki.events.emit

::: hinoki.wsgi.WSGIAdapter
    options:
      members: [start, close]
      show_if_no_docstring: true
