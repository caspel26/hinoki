---
description: "API reference for `Settings`: loading, explaining and validating application settings."
---

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
      members: [execution, lifecycle, logging, docs, server, pagination, cors, compression, metrics, events, security, admin, timezone, debug, allowed_hosts, runtime_config]
      show_if_no_docstring: true

::: hinoki.settings.ExecutionSettings
    options:
      members: [sync_workers, sync_queue, request_timeout, max_body_bytes, max_query_bytes, max_header_bytes, max_streams, stream_idle_timeout, sse_keepalive, max_websockets, ws_max_message_bytes, ws_queue, ws_ping_interval, ws_pong_timeout, ws_close_timeout]
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
      members: [title, version, openapi_url, docs_url, redoc_url, llms_url, openapi_version]
      show_if_no_docstring: true

::: hinoki.settings.ServerSettings
    options:
      members: [server, host, port, workers, runtime_threads, backlog, access_log, access_log_format, access_log_skip, access_log_slow_ms, recent_requests, server_timing, sighup, reuse_port, max_connections, header_read_timeout, body_read_timeout, write_timeout, tls_cert_file, tls_key_file, http2, native_dispatch, trusted_proxies, request_id_header, trust_request_id]
      show_if_no_docstring: true

::: hinoki.settings.PaginationSettings
    options:
      members: [page_size, max_page_size, cursor_secret, cursor_ttl_seconds]
      show_if_no_docstring: true

::: hinoki.settings.CorsSettings
    options:
      members: [origins, methods, headers, expose_headers, credentials, max_age]
      show_if_no_docstring: true

::: hinoki.settings.CompressionSettings
    options:
      members: [enabled, encodings, min_size, types, event_streams, files, gzip_level, brotli_quality, max_concurrent]
      show_if_no_docstring: true

::: hinoki.settings.MetricsSettings
    options:
      members: [listen, path, allow, token, buckets, snapshot_interval]
      show_if_no_docstring: true

::: hinoki.settings.EventsSettings
    options:
      members: [handler_timeout, max_handlers, max_depth]
      show_if_no_docstring: true

::: hinoki.settings.SecuritySettings
    options:
      members: [redirect_https, hsts_seconds, hsts_include_subdomains, hsts_preload, frame_options, referrer_policy]
      show_if_no_docstring: true

::: hinoki.settings.AdminSettings
    options:
      members: [secret, session_hours, idle_minutes, min_password_length, theme]
      show_if_no_docstring: true

::: hinoki.settings.FrameOptions

::: hinoki.settings.ReferrerPolicy

::: hinoki.settings.DatabaseSettings
    options:
      members: [url, engine, name, user, password, host, port, options, max_size]
      show_if_no_docstring: true

::: hinoki.settings.DatabaseEngine

::: hinoki.settings.EmailSettings
    options:
      members: [backend, default_sender, host, port, security, username, password, allow_insecure, tls_ca_file, helo_name, message_id_domain, connect_timeout, command_timeout, data_timeout, timeout, max_connections, pool_wait, idle_timeout, max_messages_per_connection, retries, backoff_base, backoff_max, max_recipients, max_message_bytes, max_attachments, memory_capacity, check_on_startup]
      show_if_no_docstring: true

::: hinoki.settings.EmailBackend

::: hinoki.settings.EmailSecurity
