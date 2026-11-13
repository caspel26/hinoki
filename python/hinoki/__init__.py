"""Hinoki: a typed Python API framework with a native Rust core."""

from hinoki.access import Access
from hinoki.api import API
from hinoki.app import HinokiAPI
from hinoki.errors import ModuleGraphError, RegistrationError, ValidationError
from hinoki.modules import Module, ModuleContext
from hinoki.native import core_version
from hinoki.routing import Route, delete, get, patch, post, put
from hinoki.runtime.config import RuntimeConfig
from hinoki.schema import UNSET, Field, Schema, Unset

__version__ = core_version()

__all__ = [
    "API",
    "UNSET",
    "Access",
    "Field",
    "HinokiAPI",
    "Module",
    "ModuleContext",
    "ModuleGraphError",
    "RegistrationError",
    "Route",
    "RuntimeConfig",
    "Schema",
    "Unset",
    "ValidationError",
    "__version__",
    "delete",
    "get",
    "patch",
    "post",
    "put",
]
