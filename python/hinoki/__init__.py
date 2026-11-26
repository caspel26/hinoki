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
from hinoki.computed import computed_field
from hinoki.core import core_version
from hinoki.derive import derive
from hinoki.fields import Email, Secret, Slug, Url
from hinoki.http.request import Request
from hinoki.http.response import Response, set_cookie
from hinoki.modules import Module, ModuleContext, Resource
from hinoki.parsers import (
    BytesParser,
    FormParser,
    JSONParser,
    MultiPartParser,
    Parser,
    TextParser,
    UploadedFile,
    XMLParser,
)
from hinoki.ratelimit import RateLimit, RateLimitContext, RateLimitPolicy
from hinoki.renderers import HTMLRenderer, JSONRenderer, Renderer, XMLRenderer
from hinoki.resources import (
    Crud,
    CrudAccess,
    RowScope,
    all_rows,
    crud,
    from_user,
    own_rows,
)
from hinoki.routing import Includable, Mount, Route, RouteGroup
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
    before_validate,
    field_check,
    field_normalize,
    object_check,
)
from hinoki.sources import XML, Form, Query

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
    "XML",
    "APIKeyAuth",
    "Access",
    "Actions",
    "BaseAuth",
    "BearerAuth",
    "BytesParser",
    "Caller",
    "Crud",
    "CrudAccess",
    "Email",
    "Field",
    "Form",
    "FormParser",
    "HTMLRenderer",
    "HeaderAuth",
    "Hinoki",
    "Imports",
    "Includable",
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
    "Query",
    "RateLimit",
    "RateLimitContext",
    "RateLimitPolicy",
    "Renderer",
    "Request",
    "Resource",
    "Response",
    "Route",
    "RouteGroup",
    "RowScope",
    "RuntimeConfig",
    "Schema",
    "SchemaConfig",
    "SchemaExtra",
    "Schemas",
    "Secret",
    "Slug",
    "TextParser",
    "Transactions",
    "Unset",
    "UploadedFile",
    "Url",
    "XMLParser",
    "XMLRenderer",
    "__version__",
    "all_rows",
    "before_validate",
    "computed_field",
    "crud",
    "derive",
    "errors",
    "field_check",
    "field_normalize",
    "from_user",
    "http",
    "object_check",
    "own_rows",
    "set_cookie",
    "settings",
]
