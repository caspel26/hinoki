"""Hinoki: a typed Python API framework with a native Rust core."""

from hinoki.access import Access
from hinoki.api import API
from hinoki.app import HinokiAPI
from hinoki.auth import APIKeyAuth, BaseAuth, BearerAuth, HeaderAuth, Principal
from hinoki.errors import ModuleGraphError, RegistrationError, ValidationError
from hinoki.httperrors import (
    APIError,
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    Unauthorized,
)
from hinoki.modules import Module, ModuleContext
from hinoki.native import core_version
from hinoki.parsers import FormParser, JSONParser, MultiPartParser, Parser
from hinoki.renderers import JSONRenderer, Renderer
from hinoki.request import Request
from hinoki.routing import Route, delete, get, patch, post, put
from hinoki.runtime.config import RuntimeConfig
from hinoki.schema import UNSET, Field, Schema, Unset

__version__ = core_version()

__all__ = [
    "API",
    "UNSET",
    "APIError",
    "APIKeyAuth",
    "Access",
    "BadRequest",
    "BaseAuth",
    "BearerAuth",
    "Conflict",
    "Field",
    "Forbidden",
    "FormParser",
    "HeaderAuth",
    "HinokiAPI",
    "JSONParser",
    "JSONRenderer",
    "Module",
    "ModuleContext",
    "ModuleGraphError",
    "MultiPartParser",
    "NotFound",
    "Parser",
    "Principal",
    "RegistrationError",
    "Renderer",
    "Request",
    "Route",
    "RuntimeConfig",
    "Schema",
    "Unauthorized",
    "Unset",
    "ValidationError",
    "__version__",
    "delete",
    "get",
    "patch",
    "post",
    "put",
]
