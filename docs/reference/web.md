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

::: hinoki.routing.Route
    options:
      members: true
      show_if_no_docstring: true

## HTTP method decorators

::: hinoki.routing.get

::: hinoki.routing.post

::: hinoki.routing.put

::: hinoki.routing.patch

::: hinoki.routing.delete

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
