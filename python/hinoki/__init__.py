"""Hinoki: a typed Python API framework with a native Rust core."""

from hinoki._core import core_version
from hinoki.access import Access
from hinoki.api import API
from hinoki.app import HinokiAPI, RegistrationError
from hinoki.routing import Route, delete, get, patch, post, put

__version__ = core_version()

__all__ = [
    "API",
    "Access",
    "HinokiAPI",
    "RegistrationError",
    "Route",
    "__version__",
    "delete",
    "get",
    "patch",
    "post",
    "put",
]
