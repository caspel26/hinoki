"""Hinoki: a typed Python API framework with a native Rust core."""

from hinoki import http, settings
from hinoki.access import Access
from hinoki.api import API
from hinoki.app import Hinoki
from hinoki.auth import (
    APIKeyAuth,
    BaseAuth,
    BearerAuth,
    Caller,
    HeaderAuth,
)
from hinoki.core import core_version
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
from hinoki.parsers import FormParser, JSONParser, MultiPartParser, Parser
from hinoki.renderers import HTMLRenderer, JSONRenderer, Renderer
from hinoki.reply import Reply, set_cookie
from hinoki.request import Request
from hinoki.routing import Mount, Route, RouteGroup
from hinoki.rules import Actions, Imports, Schemas, Transactions
from hinoki.runtime.config import RuntimeConfig
from hinoki.schema import (
    UNSET,
    Field,
    Invalid,
    Schema,
    Unset,
    field_check,
    object_check,
)

__version__ = core_version()

__all__ = [
    "API",
    "UNSET",
    "APIError",
    "APIKeyAuth",
    "Access",
    "Actions",
    "BadRequest",
    "BaseAuth",
    "BearerAuth",
    "Caller",
    "Conflict",
    "Field",
    "Forbidden",
    "FormParser",
    "HTMLRenderer",
    "HeaderAuth",
    "Hinoki",
    "Imports",
    "Invalid",
    "JSONParser",
    "JSONRenderer",
    "Module",
    "ModuleContext",
    "ModuleGraphError",
    "Mount",
    "MultiPartParser",
    "NotFound",
    "Parser",
    "RegistrationError",
    "Renderer",
    "Reply",
    "Request",
    "Route",
    "RouteGroup",
    "RuntimeConfig",
    "Schema",
    "Schemas",
    "Transactions",
    "Unauthorized",
    "Unset",
    "ValidationError",
    "__version__",
    "field_check",
    "http",
    "object_check",
    "set_cookie",
    "settings",
]
