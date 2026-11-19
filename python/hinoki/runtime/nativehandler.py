"""Which handlers the native handler lane may run, and the ``@native`` marker.

``native_handler_eligible`` is the one rule the Rust server uses to classify a
route; ``native_marker_problem`` applies it to a handler the author marked with
``@native``, so a marker that cannot be honoured fails at registration with the
reason instead of silently running elsewhere.
"""

from __future__ import annotations

from hinoki.errors import RegistrationError
from hinoki.nativemark import is_native
from hinoki.parsers import is_native_parser
from hinoki.renderers import is_native_renderer
from hinoki.runtime.binding import (
    BODY_METHODS,
    EMPTY,
    KINDS_BY_NAME,
    PATH_PARAM,
    Binding,
    Param,
)


def native_input_supported(param: Param) -> bool:
    """Whether Rust converts this input as the built-in converter does."""
    if param.source == "path":
        return param.default is EMPTY
    return param.source == "query" and (
        param.default is EMPTY or type(param.default) is KINDS_BY_NAME[param.kind]
    )


def native_handler_eligible(binding: Binding) -> bool:
    """Select the automatic native response path for a simple GET or DELETE, or
    for a body method with one ``Schema`` body and only built-in parsers."""
    route = binding.route
    if route.owner is not None:
        return False
    if route.method in BODY_METHODS:
        if (
            binding.body_param is None
            or binding.query_param is not None
            or not all(is_native_parser(p) for p in route.parsers)
        ):
            return False
    elif route.method not in {"GET", "DELETE"} or binding.needs_body:
        return False
    renderer_ok = all(is_native_renderer(r) for r in route.renderers)
    path_names = PATH_PARAM.findall(route.path)
    supported_inputs = all(
        (param.source in {"path", "query"} and native_input_supported(param))
        or param is binding.body_param
        or param is binding.query_param
        or (param.source == "principal" and param.name == binding.principal_name)
        for param in binding.params
    )
    bound_path_names = {
        param.name for param in binding.params if param.source == "path"
    }
    return bool(
        bound_path_names == set(path_names)
        and supported_inputs
        and not binding.takes_request
        and renderer_ok
    )


def native_marker_problem(binding: Binding) -> str | None:
    """Why a ``@native`` handler cannot run natively, or ``None``."""
    route = binding.route
    if not is_native(route.handler):
        return None
    checks = (
        (binding.is_async, "it must be an async def"),
        (
            route.owner is None,
            "it belongs to an API class or generated resource",
        ),
        (not binding.takes_request, "it takes Request"),
        (
            native_handler_eligible(binding),
            "its method, inputs, parsers or renderers are not supported by the "
            "native lane (path and query inputs, one Schema body, built-in "
            "parsers and renderers)",
        ),
    )
    return next((reason for passed, reason in checks if not passed), None)


def check_native_marker(binding: Binding) -> None:
    """Raise ``RegistrationError`` for a ``@native`` handler that cannot be one."""
    problem = native_marker_problem(binding)
    if problem is not None:
        route = binding.route
        raise RegistrationError(
            f"@native {route.name} ({route.method} {route.path}): {problem}"
        )
