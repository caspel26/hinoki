# Settings

::: hinoki.settings.Settings
    options:
      members: [load, explain, example, describe, validate]
      show_if_no_docstring: true

::: hinoki.settings.Secret
    options:
      members: [reveal]

::: hinoki.settings.SettingsError

::: hinoki.settings.FrameworkSettings
    options:
      members: [execution, lifecycle, logging, docs, server, runtime_config]
      show_if_no_docstring: true

::: hinoki.settings.ExecutionSettings
    options:
      members: [sync_workers, sync_queue, request_timeout, max_body_bytes]
      show_if_no_docstring: true

::: hinoki.settings.LifecycleSettings
    options:
      members: [startup_timeout, shutdown_timeout]
      show_if_no_docstring: true

::: hinoki.settings.LoggingSettings
    options:
      members: [level, format, apply]
      show_if_no_docstring: true

::: hinoki.settings.DocsSettings
    options:
      members: [title, version, openapi_url, docs_url, redoc_url]
      show_if_no_docstring: true

::: hinoki.settings.ServerSettings
    options:
      members: [host, port, workers, runtime_threads, backlog, access_log]
      show_if_no_docstring: true

::: hinoki.settings.DatabaseSettings
    options:
      members: [url, max_size]
      show_if_no_docstring: true
