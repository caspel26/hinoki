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

::: hinoki.routing.Mount
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.routing.Route
    options:
      members: true
      show_if_no_docstring: true

## HTTP method decorators

Import the namespace and decorate actions: `from hinoki import http`, then `@http.get(...)`. The names are not available at the top level of `hinoki`.

::: hinoki.http.get

::: hinoki.http.post

::: hinoki.http.put

::: hinoki.http.patch

::: hinoki.http.delete

## Request, response and errors

::: hinoki.request.Request
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.reply.Reply
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.reply.set_cookie

::: hinoki.access.Access
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.httperrors.APIError

::: hinoki.httperrors.BadRequest

::: hinoki.httperrors.Unauthorized

::: hinoki.httperrors.Forbidden

::: hinoki.httperrors.NotFound

::: hinoki.httperrors.Conflict
