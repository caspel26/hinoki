"""Hinoki: a typed Python API framework with a native Rust core."""

import warnings
from typing import TYPE_CHECKING

from hinoki import errors, http, settings
from hinoki.access import Access
from hinoki.api import API
from hinoki.app import Hinoki
from hinoki.auth import (
    APIKeyAuth,
    BaseAuth,
    BearerAuth,
    Caller,
    HeaderAuth,
    Permission,
)
from hinoki.core import core_version
from hinoki.crud import Crud, CrudAPI
from hinoki.derive import derive
from hinoki.http.request import Request
from hinoki.http.response import Response, set_cookie
from hinoki.modules import Module, ModuleContext
from hinoki.parsers import FormParser, JSONParser, MultiPartParser, Parser, UploadedFile
from hinoki.renderers import HTMLRenderer, JSONRenderer, Renderer
from hinoki.routing import Mount, Route, RouteGroup
from hinoki.rules import Actions, Imports, Models, Schemas, Transactions
from hinoki.runtime.config import RuntimeConfig
from hinoki.schema import (
    UNSET,
    Field,
    Invalid,
    Schema,
    SchemaConfig,
    SchemaExtra,
    Unset,
    field_check,
    field_normalize,
    object_check,
)

__version__ = core_version()

if TYPE_CHECKING:
    # Deprecated alias for one compatibility cycle; ``__getattr__`` warns at
    # runtime. Type checkers see only this name, not a catch-all ``__getattr__``.
    Reply = Response
else:

    def __getattr__(name: str) -> object:
        """Resolve the deprecated ``Reply`` alias with a warning."""
        if name == "Reply":
            warnings.warn(
                "hinoki.Reply is deprecated; use hinoki.http.response.Response "
                "(also exported as hinoki.Response)",
                DeprecationWarning,
                stacklevel=2,
            )
            return Response
        raise AttributeError(f"module 'hinoki' has no attribute {name!r}")


__all__ = [
    "API",
    "UNSET",
    "APIKeyAuth",
    "Access",
    "Actions",
    "BaseAuth",
    "BearerAuth",
    "Caller",
    "Crud",
    "CrudAPI",
    "Field",
    "FormParser",
    "HTMLRenderer",
    "HeaderAuth",
    "Hinoki",
    "Imports",
    "Invalid",
    "JSONParser",
    "JSONRenderer",
    "Models",
    "Module",
    "ModuleContext",
    "Mount",
    "MultiPartParser",
    "Parser",
    "Permission",
    "Renderer",
    "Request",
    "Response",
    "Route",
    "RouteGroup",
    "RuntimeConfig",
    "Schema",
    "SchemaConfig",
    "SchemaExtra",
    "Schemas",
    "Transactions",
    "Unset",
    "UploadedFile",
    "__version__",
    "derive",
    "errors",
    "field_check",
    "field_normalize",
    "http",
    "object_check",
    "set_cookie",
    "settings",
]
