"""Every public error type: HTTP-facing ``APIError`` subclasses, the registry that
translates other exceptions, and the framework's own failures.

Use it as a namespace: ``from hinoki import errors`` then ``errors.NotFound``.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, cast

from hinoki.core import UniqueViolation


class RegistrationError(Exception):
    """Invalid or conflicting declarations, raised before the app serves."""


class ModuleGraphError(RegistrationError):
    """Missing dependency, dependency cycle or duplicate module name."""


STARTUP_HOOK = "on_startup:"


class StartupError(Exception):
    """A module or a startup hook failed. Carries its name (``module``; a hook
    is ``on_startup:<function>``), never settings values."""

    def __init__(self, module: str, cause: BaseException) -> None:
        what = (
            f"startup hook {module.removeprefix(STARTUP_HOOK)!r}"
            if module.startswith(STARTUP_HOOK)
            else f"module {module!r}"
        )
        super().__init__(f"{what} failed to start: {type(cause).__name__}")
        self.module = module
        self.cause = cause


class ShutdownError(Exception):
    """One or more cleanup steps failed; every step was still attempted."""

    def __init__(self, causes: list[BaseException]) -> None:
        names = ", ".join(type(c).__name__ for c in causes)
        super().__init__(f"shutdown finished with errors: {names}")
        self.causes = causes


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """One validation problem: where, a stable code, and a readable message."""

    path: str
    code: str
    message: str


class ValidationError(Exception):
    """Input does not match a schema. ``details`` lists every problem found."""

    def __init__(self, details: list[ErrorDetail]) -> None:
        super().__init__(f"{len(details)} validation error(s)")
        self.details = details


class InvalidBody(Exception):
    """The request body cannot be used at all (wrong media type, bad JSON)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ModelError(Exception):
    """Base class for ORM errors that make sense without HTTP."""


class ObjectNotFound(ModelError):
    """A query that must return one row returned none. Each model has its own
    subclass as ``Model.NotFound``; HTTP maps it to 404."""


class MultipleObjectsReturned(ModelError):
    """A single-row query or relation returned more than one row."""


class FieldError(ModelError, ValueError):
    """A query or write names a field the model does not have."""


class ModelValidationError(ValidationError, ModelError):
    """One or more Model field values failed before a database write."""


class RelationNotLoaded(ModelError, AttributeError):
    """A relation was read that the query did not load. Relations are never
    fetched lazily; name them in ``select_related`` or ``prefetch_related``."""


class MigrationError(Exception):
    """A migration cannot be planned, verified or applied."""


class DestructiveMigration(MigrationError):
    """The plan loses data or may fail on existing rows and was not allowed."""


class APIError(Exception):
    """An error with a public HTTP representation.

    Subclasses set ``status``, ``code`` and ``message``; an instance may
    override them, which is how a handler answers with a status of its own:
    ``APIError("Card declined", status=402, code="card_declined")``.
    ``details`` is public data, never internal state.
    """

    status: int = HTTPStatus.INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "Internal server error."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status: int | None = None,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self.message = message or type(self).message
        self.status = status or type(self).status
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


MAX_RETRY_AFTER = 86_400  # seconds: a day is the longest wait a client is told


class TooManyRequests(APIError):
    """The one error a ``rate_limit`` policy may raise to refuse a request.

    ``retry_after`` is a whole number of seconds from 1 to 86,400, sent as
    ``Retry-After``; anything else is refused when the error is created."""

    status = HTTPStatus.TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests."

    def __init__(
        self, retry_after: int, message: str | None = None, *, code: str | None = None
    ) -> None:
        if type(retry_after) is not int or not 1 <= retry_after <= MAX_RETRY_AFTER:
            raise ValueError(
                f"retry_after must be whole seconds from 1 to {MAX_RETRY_AFTER}"
            )
        super().__init__(message, code=code, headers={"retry-after": str(retry_after)})
        self.retry_after = retry_after


Handler = Callable[[Any], APIError | Awaitable[APIError]]


class UniqueConflict(Conflict):
    """A unique value is already in use: the answer to a ``UniqueViolation``."""

    code = "unique_conflict"
    message = "A unique value is already in use."


def not_found(_error: ObjectNotFound) -> APIError:
    return NotFound()


def unique_conflict(_error: UniqueViolation) -> APIError:
    return UniqueConflict()


def checked_handler(kind: object, handler: object) -> Handler:
    """``handler`` if a function can answer ``kind``, else ``TypeError``."""
    if not (isinstance(kind, type) and issubclass(kind, Exception)):
        raise TypeError(f"{kind!r} is not an exception class")
    if isinstance(handler, type) or not callable(handler):
        raise TypeError(f"{handler!r} must be a function of the exception")
    return cast(Handler, handler)


class ErrorRegistrar:
    """Decorator returned by ``app.errors.handle(SomeError)``."""

    def __init__(self, registry: ErrorRegistry, kind: type[Exception]) -> None:
        self.registry = registry
        self.kind = kind

    def __call__[T: Handler](self, handler: T) -> T:
        self.registry.add(self.kind, handler)
        return handler


class ErrorRegistry:
    """Maps exception classes to handlers; the most specific class wins."""

    def __init__(self) -> None:
        self.handlers: dict[type[Exception], Handler] = {}
        # Built-in translations, consulted after the application's handlers.
        self.defaults: dict[type[Exception], Handler] = {
            ObjectNotFound: not_found,
            UniqueViolation: unique_conflict,
        }

    def handle(self, kind: type[Exception]) -> ErrorRegistrar:
        return ErrorRegistrar(self, kind)

    def add(self, kind: type[Exception], handler: Handler) -> None:
        checked = checked_handler(kind, handler)
        if kind in self.handlers:
            raise ValueError(f"a handler for {kind.__name__} is already registered")
        self.handlers[kind] = checked

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
