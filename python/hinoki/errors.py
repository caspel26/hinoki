"""Framework error types that are independent of HTTP."""

from __future__ import annotations

from dataclasses import dataclass


class RegistrationError(Exception):
    """Invalid or conflicting declarations, raised before the app serves."""


class ModuleGraphError(RegistrationError):
    """Missing dependency, dependency cycle or duplicate module name."""


class StartupError(Exception):
    """A module failed to start. Carries the module name, never settings values."""

    def __init__(self, module: str, cause: BaseException) -> None:
        super().__init__(f"module {module!r} failed to start: {type(cause).__name__}")
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
