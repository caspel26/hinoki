"""Framework error types that are independent of HTTP."""

from __future__ import annotations


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
