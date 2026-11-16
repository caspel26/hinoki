"""Build the native request classifier (``core.ServerCore``) from a pipeline.

The classifier answers what needs no Python (not ready, oversized requests,
overload, and, when no host/CORS/HTTPS-redirect policy is configured, unknown
paths and wrong methods) and queues everything else for the pipeline. Everything
it needs is computed here once, at startup, from the same objects the Python
path uses, so the two cannot describe different applications.
"""

from __future__ import annotations

from http import HTTPStatus

from hinoki import core
from hinoki.context import request_id
from hinoki.runtime.binding import PATH_PARAM, Binding
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.responses import RETRY_LATER, HTTPError

# Rendered request id used as a splice point; mirrors `hinoki_server::early::SENTINEL`.
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
}


def early_bodies() -> list[tuple[str, bytes]]:
    """The JSON body of each early answer, rendered by ``HTTPError`` itself with
    the sentinel as request id."""
    token = request_id.set(SENTINEL)
    try:
        return [
            (kind, HTTPError(status, error, headers).response.body)
            for kind, (status, error, headers) in EARLY_ERRORS.items()
        ]
    finally:
        request_id.reset(token)


def route_spec(action: int, binding: Binding) -> core.RouteSpec:
    route = binding.route
    owner = route.owner
    return (
        action,
        route.method,
        route.path,
        PATH_PARAM.findall(route.path),
        binding.needs_body,
        route.access is not None,
        [renderer.media_type for renderer in route.renderers],
        int(route.status),
        [(str(k), str(v)) for k, v in getattr(owner, "response_headers", ())],
        route.transaction,
        route.is_async,
        binding.takes_request,
    )


def cors_of(pipeline: Pipeline) -> tuple[bool, list[bytes], bool, bytes] | None:
    """The CORS policy as the classifier needs it to decorate its own answers."""
    policy = pipeline.cors
    if policy is None:
        return None
    origins = sorted(origin.encode("ascii") for origin in policy.origins)
    return policy.any_origin, origins, policy.credentials, policy.expose


def build_server_core(pipeline: Pipeline) -> core.ServerCore:
    """The classifier for one worker. It starts not ready; the lifespan flips it."""
    app = pipeline.app
    runtime = app.runtime
    defer = (
        pipeline.hosts is not None
        or pipeline.cors is not None
        or pipeline.security.redirect_https
    )
    python_paths = [*pipeline.docs]
    if pipeline.document is not None and app.openapi_url is not None:
        python_paths.append(app.openapi_url)
    return core.ServerCore(
        [route_spec(i, b) for i, b in enumerate(pipeline.bindings)],
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
        trust_request_id=pipeline.trust_request_id,
        defer_policies=defer,
        python_paths=python_paths,
        cors=cors_of(pipeline),
        # Canonical CIDRs from the same objects the Python path matches against.
        trusted_proxies=[str(network) for network in pipeline.trusted_proxies],
    )
