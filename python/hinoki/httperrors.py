"""HTTP-facing errors and the registry that translates other exceptions."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from hinoki.errors import ObjectNotFound


class APIError(Exception):
    """An error with a public HTTP representation.

    Subclasses set ``status``, ``code`` and ``message``; an instance may
    override them. ``details`` is public data, never internal state.
    """

    status: int = HTTPStatus.INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "Internal server error."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self.message = message or type(self).message
        self.code = code or type(self).code
        self.details = details
        self.headers = headers or {}


class BadRequest(APIError):
    status = HTTPStatus.BAD_REQUEST
    code = "bad_request"
    message = "The request is invalid."


class Unauthorized(APIError):
    status = HTTPStatus.UNAUTHORIZED
    code = "unauthorized"
    message = "Authentication is required."


class Forbidden(APIError):
    status = HTTPStatus.FORBIDDEN
    code = "forbidden"
    message = "You are not allowed to do this."


class NotFound(APIError):
    status = HTTPStatus.NOT_FOUND
    code = "not_found"
    message = "Not found."


class Conflict(APIError):
    status = HTTPStatus.CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state."


Handler = Callable[[Any], APIError | Awaitable[APIError]]


def not_found(_error: ObjectNotFound) -> APIError:
    return NotFound()


class ErrorRegistrar:
    """Decorator returned by ``app.errors.handle(SomeError)``."""

    def __init__(self, registry: ErrorRegistry, kind: type[Exception]) -> None:
        self.registry = registry
        self.kind = kind

    def __call__(self, handler: Handler) -> Handler:
        self.registry.add(self.kind, handler)
        return handler


class ErrorRegistry:
    """Maps exception classes to handlers; the most specific class wins."""

    def __init__(self) -> None:
        self.handlers: dict[type[Exception], Handler] = {}
        # Built-in translations, consulted after the application's handlers.
        self.defaults: dict[type[Exception], Handler] = {ObjectNotFound: not_found}

    def handle(self, kind: type[Exception]) -> ErrorRegistrar:
        return ErrorRegistrar(self, kind)

    def add(self, kind: type[Exception], handler: Handler) -> None:
        if kind in self.handlers:
            raise ValueError(f"a handler for {kind.__name__} is already registered")
        self.handlers[kind] = handler

    def find(self, exc: Exception) -> Handler | None:
        for table in (self.handlers, self.defaults):
            for klass in type(exc).__mro__:
                handler = table.get(klass)
                if handler is not None:
                    return handler
        return None

    async def translate(self, exc: Exception) -> APIError | None:
        """The public error for ``exc``, or ``None`` if nothing handles it.

        A handler that fails or returns something else is treated as unhandled.
        """
        handler = self.find(exc)
        if handler is None:
            return None
        try:
            result = handler(exc)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return None
        return result if isinstance(result, APIError) else None
