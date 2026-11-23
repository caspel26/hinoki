# Core API: application, routing and HTTP

`from hinoki import Hinoki, RouteGroup, Mount, http, errors` and `from hinoki.project import manifest_routes`. Guides: [function-based APIs](../function-endpoints.md), [class-based APIs](../class-endpoints.md), [register routes](../routes.md), [errors](../errors.md).

::: hinoki.app.Hinoki
    options:
      show_if_no_docstring: true
      inherited_members: false

::: hinoki.api.API
    options:
      show_if_no_docstring: true

::: hinoki.routing.RouteGroup
    options:
      show_if_no_docstring: true

::: hinoki.routing.Includable

::: hinoki.routing.Mount
    options:
      show_if_no_docstring: true

::: hinoki.routing.Route
    options:
      show_if_no_docstring: true

::: hinoki.project.manifest_routes

::: hinoki.project.manifest_events

::: hinoki.project.manifest_middleware

::: hinoki.project.manifest_rules

`Hinoki()` loads the routes, events, middleware and rules of `hinoki.toml` by
itself; call these functions only to pass the same lists by hand.
For model-backed route generation, see [Generate CRUD routes](../crud.md) and
the [CRUD API reference](crud.md).

## HTTP method decorators

Import the namespace and decorate actions: `from hinoki import http`, then `@http.get(...)`. The names are not available at the top level of `hinoki`.

::: hinoki.http.get

::: hinoki.http.post

::: hinoki.http.put

::: hinoki.http.patch

::: hinoki.http.delete

::: hinoki.http.query

## Request, response and errors

The types are available from `hinoki.http`, from `hinoki`, or from their modules `hinoki.http.request` and `hinoki.http.response`.

::: hinoki.http.request.Request
    options:
      show_if_no_docstring: true

::: hinoki.http.response.Response
    options:
      show_if_no_docstring: true

::: hinoki.http.response.set_cookie

::: hinoki.http.Stream
    options:
      show_if_no_docstring: true

::: hinoki.http.EventStream
    options:
      show_if_no_docstring: true

::: hinoki.http.Event
    options:
      show_if_no_docstring: true

::: hinoki.http.File
    options:
      show_if_no_docstring: true

::: hinoki.access.Access
    options:
      show_if_no_docstring: true

::: hinoki.errors.APIError

::: hinoki.errors.BadRequest

::: hinoki.errors.Unauthorized

::: hinoki.errors.Forbidden

::: hinoki.errors.NotFound

::: hinoki.errors.Conflict

::: hinoki.errors.TooManyRequests

::: hinoki.errors.UniqueConflict

::: hinoki.errors.ObjectNotFound

::: hinoki.errors.ErrorRegistry

## Middleware

A project-wide middleware subclasses `Middleware`; see the [middleware
guide](../middleware.md). List it in `middleware` of `[project]` in `hinoki.toml`
or pass `middleware=[...]` to `Hinoki`.

::: hinoki.http.Middleware
    options:
      show_if_no_docstring: true

::: hinoki.http.MiddlewareRequest
    options:
      show_if_no_docstring: true

## Rate limits

::: hinoki.ratelimit.RateLimitPolicy

::: hinoki.ratelimit.RateLimitContext
