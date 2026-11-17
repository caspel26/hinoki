# Settings

::: hinoki.settings.Settings
    options:
      members: [load, explain, example, describe, validate]
      show_if_no_docstring: true

::: hinoki.settings.Secret
    options:
      members: [reveal]

::: hinoki.settings.SettingsError

::: hinoki.settings.AppSettings
    options:
      members: [execution, lifecycle, logging, docs, server, pagination, cors, security, timezone, debug, allowed_hosts, runtime_config]
      show_if_no_docstring: true

::: hinoki.settings.ExecutionSettings
    options:
      members: [sync_workers, sync_queue, request_timeout, max_body_bytes, max_query_bytes, max_header_bytes]
      show_if_no_docstring: true

::: hinoki.settings.LifecycleSettings
    options:
      members: [startup_timeout, shutdown_timeout]
      show_if_no_docstring: true

::: hinoki.settings.LoggingSettings
    options:
      members: [level, format, include_request_id, apply]
      show_if_no_docstring: true

::: hinoki.settings.DocsSettings
    options:
      members: [title, version, openapi_url, docs_url, redoc_url]
      show_if_no_docstring: true

::: hinoki.settings.ServerSettings
    options:
      members: [server, host, port, workers, runtime_threads, backlog, access_log, max_connections, header_read_timeout, body_read_timeout, trusted_proxies, request_id_header, trust_request_id]
      show_if_no_docstring: true

::: hinoki.settings.PaginationSettings
    options:
      members: [page_size, max_page_size]
      show_if_no_docstring: true

::: hinoki.settings.CorsSettings
    options:
      members: [origins, methods, headers, expose_headers, credentials, max_age]
      show_if_no_docstring: true

::: hinoki.settings.SecuritySettings
    options:
      members: [redirect_https, hsts_seconds, hsts_include_subdomains, hsts_preload, frame_options, referrer_policy]
      show_if_no_docstring: true

::: hinoki.settings.FrameOptions

::: hinoki.settings.ReferrerPolicy

::: hinoki.settings.DatabaseSettings
    options:
      members: [url, engine, name, user, password, host, port, options, max_size]
      show_if_no_docstring: true

::: hinoki.settings.DatabaseEngine
