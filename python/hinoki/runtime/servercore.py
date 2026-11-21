"""Build the native request classifier (``core.ServerCore``) from a pipeline.

The classifier answers what needs no Python (not ready, oversized requests,
overload, and, when no host/CORS/HTTPS-redirect policy is configured, unknown
paths and wrong methods) and queues everything else for the pipeline. Everything
it needs is computed here once, at startup, from the same objects the Python
path uses, so the two cannot describe different applications.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from hinoki import core
from hinoki.context import request_id
from hinoki.errors import Forbidden, Unauthorized
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
from hinoki.runtime.crudplan import (
    CrudPlanSelection,
    auth_snapshot,
    claims_access_is_plain,
    native_claims_auth,
    select_crud_plan,
)
from hinoki.runtime.pipeline import Pipeline, docs_response
from hinoki.runtime.responses import RETRY_LATER, HTTPError, Response, public_error

log = logging.getLogger("hinoki.server")

# Rendered request id used as a splice point; mirrors `kiso::early::SENTINEL`.
SENTINEL = "0123456789abcdeffedcba9876543210"

EARLY_ERRORS: dict[str, tuple[int, str, list[tuple[bytes, bytes]] | None]] = {
    "uri_too_long": (HTTPStatus.REQUEST_URI_TOO_LONG, "uri_too_long", None),
    "headers_too_large": (
        HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
        "headers_too_large",
        None,
    ),
    "not_ready": (HTTPStatus.SERVICE_UNAVAILABLE, "not_ready", RETRY_LATER),
    "overloaded": (HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER),
    "not_found": (HTTPStatus.NOT_FOUND, "not_found", None),
    # The `allow` header is added per request by the classifier.
    "method_not_allowed": (
        HTTPStatus.METHOD_NOT_ALLOWED,
        "method_not_allowed",
        None,
    ),
    # HTTP/1.1 without exactly one Host header (RFC 9112 3.2).
    "bad_host": (HTTPStatus.BAD_REQUEST, "bad_host", None),
    "disallowed_host": (HTTPStatus.BAD_REQUEST, "disallowed_host", None),
    "cors_origin_denied": (HTTPStatus.FORBIDDEN, "cors_origin_denied", None),
}


def early_bodies() -> list[tuple[str, bytes]]:
    """The JSON body of each early answer, rendered by ``HTTPError`` itself with
    the sentinel as request id."""
    token = request_id.set(SENTINEL)
    try:
        rendered = [
            (kind, HTTPError(status, error, headers).response.body)
            for kind, (status, error, headers) in EARLY_ERRORS.items()
        ]
        rendered.extend(
            [
                (
                    "invalid_query",
                    HTTPError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_query",
                        message="malformed query string",
                    ).response.body,
                ),
                (
                    "not_acceptable",
                    HTTPError(
                        HTTPStatus.NOT_ACCEPTABLE,
                        "not_acceptable",
                        available="application/json",
                    ).response.body,
                ),
                *(
                    (kind, public_error(error).body)
                    for kind, error in (
                        ("unauthorized", Unauthorized()),
                        ("invalid_credentials", Unauthorized("Invalid credentials.")),
                        ("forbidden", Forbidden()),
                    )
                ),
            ]
        )
        return rendered
    finally:
        request_id.reset(token)


def claims_route_access(binding: Binding) -> Any:
    """The route's access policy when Rust can authenticate it, else ``None``."""
    access = binding.route.access
    if (
        access is not None
        and native_handler_eligible(binding)
        and claims_access_is_plain(access)
    ):
        return access
    return None


def route_auth_state(access: Any) -> tuple[object, ...]:
    """What Rust copied from the provider, to notice a later change."""
    return (auth_snapshot(access), access.auth.challenge)


def route_auth_of(
    pipeline: Pipeline,
) -> list[tuple[int, core._ClaimsAuth, bytes | None]]:
    """The claims-only provider of every native route that Rust authenticates,
    with the challenge Python puts on its ``401``."""
    found = []
    for action, binding in enumerate(pipeline.bindings):
        access = claims_route_access(binding)
        claims = native_claims_auth(access) if access is not None else None
        if claims is not None:
            challenge = access.auth.challenge
            found.append((action, claims, challenge.encode() if challenge else None))
    return found


def route_auth_stale(binding: Binding, access: Any, state: tuple[object, ...]) -> bool:
    """Whether the route's access or its provider no longer is what Rust holds."""
    return (
        binding.route.access is not access
        or claims_route_access(binding) is None
        or route_auth_state(access) != state
    )


def route_spec(
    action: int, binding: Binding, crud_plan: core._CrudPlan | None
) -> core.RouteSpec:
    route = binding.route
    owner = route.owner
    native_handler = native_handler_eligible(binding)
    native_inputs = (
        [
            (param.source, param.name, param.default is not EMPTY, param.kind)
            for param in binding.params
            if param.source not in {"body", "principal", "queryschema"}
        ]
        if native_handler
        else []
    )
    return (
        action,
        route.method,
        route.path,
        binding.needs_body,
        route.access is not None,
        [renderer.media_type for renderer in route.renderers],
        int(route.status),
        [(str(k), str(v)) for k, v in getattr(owner, "response_headers", ())],
        route.atomic,
        binding.takes_request,
        (native_handler, native_inputs),
        crud_plan,
    )


def native_input_supported(param: Param) -> bool:
    """Whether Rust converts this input as the built-in converter does."""
    if param.source == "path":
        return param.default is EMPTY
    return param.source == "query" and (
        param.default is EMPTY or type(param.default) is KINDS_BY_NAME[param.kind]
    )


def route_guarded(binding: Binding) -> bool:
    """Whether Python must see the headers or the peer before the handler: for
    credentials, the rate limit, or the ``Accept`` choice among renderers."""
    route = binding.route
    return (
        route.access is not None
        or route.rate_limit is not None
        or binding.query_param is not None
        or len(route.renderers) > 1
    )


def stock_owner(owner: type) -> bool:
    """Creating the class-API instance runs no application code: it keeps
    ``object``'s constructor, so a request may build it wherever its handler is
    driven."""
    return type(owner) is type and all(
        "__init__" not in vars(base) and "__new__" not in vars(base)
        for base in owner.__mro__[:-1]
    )


def native_handler_eligible(binding: Binding) -> bool:
    """Select the automatic native response path for a simple GET or DELETE, or
    for a body method with one ``Schema`` body and only built-in parsers."""
    route = binding.route
    if (
        binding.streams
        or route.middleware
        or (
            route.owner is not None
            and not (
                stock_owner(route.owner)
                and not getattr(route.owner, "response_headers", ())
            )
        )
    ):
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


def cors_of(
    pipeline: Pipeline,
) -> tuple[bool, list[bytes], bool, bytes, list[tuple[bytes, bytes]]] | None:
    """The CORS policy as the classifier needs it to decorate its own answers."""
    policy = pipeline.cors
    if policy is None:
        return None
    origins = sorted(origin.encode("ascii") for origin in policy.origins)
    return (
        policy.any_origin,
        origins,
        policy.credentials,
        policy.expose,
        list(policy.preflight),
    )


def hosts_of(pipeline: Pipeline) -> tuple[list[bytes], list[bytes]] | None:
    """The normalized host policy as immutable ASCII byte snapshots."""
    policy = pipeline.hosts
    if policy is None:
        return None
    return (
        sorted(host.encode("ascii") for host in policy.exact),
        [host.encode("ascii") for host in policy.suffixes],
    )


def static_pages_of(
    pipeline: Pipeline,
) -> list[tuple[str, list[tuple[bytes, bytes]], bytes]]:
    """The OpenAPI document and documentation pages as the answers the Python
    pipeline would give, so Rust can serve them without crossing into Python."""
    pages = []
    url = pipeline.app.openapi_url
    if pipeline.document is not None and url is not None:
        answer = Response.json(HTTPStatus.OK, pipeline.document)
        pages.append((url, list(answer.headers), answer.body))
    for path, page in pipeline.docs.items():
        answer = docs_response(page)
        pages.append((path, list(answer.headers), answer.body))
    return pages


def build_server_core(pipeline: Pipeline) -> core.ServerCore:
    """The classifier for one worker. It starts not ready; the lifespan flips it."""
    app = pipeline.app
    runtime = app.runtime
    python_paths = [*pipeline.docs]
    if pipeline.document is not None and app.openapi_url is not None:
        python_paths.append(app.openapi_url)
    selections: list[CrudPlanSelection | None] = []
    reasons: list[str] = []
    routes = []
    for index, binding in enumerate(pipeline.bindings):
        try:
            selection, reason = select_crud_plan(binding, pipeline)
        except Exception:
            log.warning(
                "native CRUD plan selection failed for %s %s; using the Python path",
                binding.route.method,
                binding.route.path,
                exc_info=True,
            )
            selection, reason = None, "native_plan_build_failed"
        selections.append(selection)
        reasons.append(reason)
        routes.append(route_spec(index, binding, selection.plan if selection else None))
    pipeline.crud_plan_selections = selections
    pipeline.crud_plan_reasons = reasons
    return core.ServerCore(
        routes,
        early_bodies(),
        (pipeline.policy[False], pipeline.policy[True]),
        pipeline.request_id_name,
        pipeline.request_id_prefix,
        limits=(
            runtime.max_query_bytes,
            runtime.max_header_bytes,
            runtime.max_body_bytes,
            4 * (runtime.sync_workers + runtime.sync_queue),
        ),
        request_timeout_ms=max(1, round(runtime.request_timeout * 1000)),
        trust_request_id=pipeline.trust_request_id,
        python_paths=python_paths,
        cors=cors_of(pipeline),
        # Canonical CIDRs from the same objects the Python path matches against.
        trusted_proxies=[str(network) for network in pipeline.trusted_proxies],
        startup_path=pipeline.startup_url,
        startup_body=(
            pipeline.startup_response.body
            if pipeline.startup_response is not None
            else b""
        ),
        hosts=hosts_of(pipeline),
        redirect_https=pipeline.security.redirect_https,
        static_pages=static_pages_of(pipeline),
        route_auth=route_auth_of(pipeline),
    )
