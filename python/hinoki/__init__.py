"""Hinoki: a typed Python API framework with a native Rust core."""

from hinoki.access import Access
from hinoki.api import API
from hinoki.app import HinokiAPI
from hinoki.errors import ModuleGraphError, RegistrationError
from hinoki.modules import Module, ModuleContext
from hinoki.native import core_version
from hinoki.routing import Route, delete, get, patch, post, put
from hinoki.runtime.config import RuntimeConfig

__version__ = core_version()

__all__ = [
    "API",
    "Access",
    "HinokiAPI",
    "Module",
    "ModuleContext",
    "ModuleGraphError",
    "RegistrationError",
    "Route",
    "RuntimeConfig",
    "__version__",
    "delete",
    "get",
    "patch",
    "post",
    "put",
]
