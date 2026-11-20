# Core API: application, routing and HTTP

::: hinoki.app.Hinoki
    options:
      members: true
      show_if_no_docstring: true
      inherited_members: false

::: hinoki.api.API
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.routing.RouteGroup
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.routing.Includable

::: hinoki.routing.Mount
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.routing.Route
    options:
      members: true
      show_if_no_docstring: true

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
      members: true
      show_if_no_docstring: true

::: hinoki.http.response.Response
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.http.response.set_cookie

::: hinoki.access.Access
    options:
      members: true
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

## Rate limits

::: hinoki.ratelimit.RateLimitPolicy

::: hinoki.ratelimit.RateLimitContext
