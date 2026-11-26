"""Schema hook decorators, sentinels, and validation exceptions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hinoki.errors import RegistrationError


class _ConfigOmitted:
    """Private marker used to type the omitted class config keyword."""


_CONFIG_UNSET = _ConfigOmitted()
_SUPPLIED_FIELDS = "__hinoki_supplied_fields__"
_TRACKS_SUPPLIED_FIELDS = "__hinoki_tracks_supplied_fields__"
_TRUSTED_CONSTRUCTOR = "__hinoki_construct_validated__"
_MODEL_CONSTRUCTOR = "__hinoki_construct_from_model__"
_INSTANCE_MISSING = object()
_INVALID_INSTANCE_VALUE = object()


class ForwardReference(Exception):
    """Internal: an annotation names a class that is not defined yet."""


class Invalid(Exception):
    """Raised by a check to refuse a value: a stable ``code``, a readable
    ``message`` and optionally the ``path`` it concerns (default: the field the
    check is attached to, or the object itself)."""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


CHECK_ATTR = "__hinoki_check__"
NORMALIZE_ATTR = "__hinoki_normalize__"
BEFORE_ATTR = "__hinoki_before__"
MAX_BEFORE_DEPTH = 64
NORMALIZER_PARAMETER_COUNT = 2


def descriptor_function(value: Any) -> Any:
    """Unwrap a staticmethod or classmethod descriptor to its function."""
    return value.__func__ if isinstance(value, (staticmethod, classmethod)) else value


def stop_awaitable(value: Any) -> None:
    """Dispose an accidental awaitable without scheduling it."""
    close = getattr(value, "close", None)
    cancel = getattr(value, "cancel", None)
    if callable(close):
        close()
    if callable(cancel):
        cancel()


def field_check[F: Callable[..., Any]](*names: str) -> Callable[[F], F]:
    """Mark a method as a check on one or more fields: ``def ok(self, value)``.

    It runs after the structural validation succeeded, so ``value`` already has
    the declared type, and may read the other fields through ``self``. Raise
    ``Invalid(code, message)`` to refuse; it never changes the value. Checks are
    synchronous and must not do I/O: a rule that needs the database belongs in an
    operation, not in a schema."""
    if not names:
        raise RegistrationError("@field_check needs at least one field name")

    def mark(function: F) -> F:
        setattr(function, CHECK_ATTR, ("field", names))
        return function

    return mark


def object_check[F: Callable[..., Any]](function: F) -> F:
    """Mark a method as a check on the whole object: ``def ok(self)``. Runs after
    every field check passed or failed (all problems are reported together)."""
    setattr(function, CHECK_ATTR, ("object", ()))
    return function


def before_validate[F: Callable[..., Any]](function: F) -> F:
    """Mark a classmethod or staticmethod that reshapes raw wire input:
    ``def prepare(cls, data: dict[str, Any]) -> dict[str, Any]``.

    It runs on the decoded mapping, before any structural validation, for the
    schema and for every schema nested in it (lists, dicts, tuples, optionals and
    discriminated variants). Return the mapping to validate. The hook gets a shallow
    copy: replace nested values instead of mutating them, or the caller's input
    changes. Raise ``Invalid(code, message)`` to refuse; a ``KeyError``,
    ``TypeError``, ``ValueError``, ``IndexError`` or ``AttributeError`` caused by
    unexpected input answers as ``invalid_input``. The input
    is untrusted: check types before relying on them, stay synchronous and do no
    I/O. Plain unions of schemas that use it are refused: use a discriminator."""
    target = descriptor_function(function)
    if target is function:
        raise RegistrationError(
            "@before_validate must wrap a classmethod or staticmethod"
        )
    setattr(target, BEFORE_ATTR, True)
    return function


def field_normalize[F: Callable[..., str]](name: str) -> Callable[[F], F]:
    """Mark a typed field canonicalizer: ``def trim(self, value: str) -> str``.

    Methods run synchronously after structural validation and return a
    replacement string; raise ``Invalid`` only from a check hook to reject.
    """
    if not isinstance(name, str) or not name:
        raise RegistrationError("@field_normalize needs one non-empty field name")

    def mark(function: F) -> F:
        setattr(function, NORMALIZE_ATTR, name)
        return function

    return mark
