"""What each error code means and how to fix it (for people and for agents).

``explain_error`` in the development MCP and the docs read this catalogue. Codes
are the stable strings clients see in ``error`` and in validation ``details``.
"""

from __future__ import annotations

CATALOG: dict[str, tuple[str, str]] = {
    "validation_error": (
        "A request body or parameter failed validation (HTTP 422); `details` lists every problem with its path.",
        "Fix the listed fields; the client sent a wrong type, a missing field, an unknown field or a value outside the schema's constraints.",
    ),
    "ge": (
        "A number is below the schema's minimum (`ge`).",
        "Send a value greater than or equal to the limit shown in the message.",
    ),
    "le": (
        "A number is above the schema's maximum (`le`).",
        "Send a value less than or equal to the limit shown in the message.",
    ),
    "gt": (
        "A number is not greater than the schema's exclusive lower limit (`gt`).",
        "Send a value strictly greater than the limit shown in the message.",
    ),
    "lt": (
        "A number is not less than the schema's exclusive upper limit (`lt`).",
        "Send a value strictly less than the limit shown in the message.",
    ),
    "bad_request": (
        "The request is malformed in a way no more specific code covers (HTTP 400).",
        "Read the message; correct the request.",
    ),
    "conflict": (
        "The request conflicts with the current state (HTTP 409), for example a duplicate.",
        "Raise a named `Conflict` subclass with its own `code` for each business rule so clients can tell the cases apart; map database unique violations once with `app.errors.handle(UniqueViolation)`.",
    ),
    "unique_conflict": (
        "A unique value is already in use (the blog example's mapping of `UniqueViolation`).",
        "Choose another value; in your own app map `UniqueViolation` to a named `Conflict`.",
    ),
    "required": (
        "A required field is missing.",
        "Send the field, or give it a default in the schema.",
    ),
    "type": (
        "The value has the wrong JSON type (no coercion happens).",
        'Send the exact type: a string stays a string, 1 is not "1".',
    ),
    "extra_forbidden": (
        "The body has a field the schema does not declare.",
        "Remove it, or declare it in the schema if clients are meant to send it.",
    ),
    "null": (
        "`null` was sent for a field that is not nullable.",
        "Omit the field or declare it `T | None`.",
    ),
    "min_length": (
        "A string or list is shorter than the schema allows.",
        "Send more characters or items.",
    ),
    "max_length": (
        "A string or list is longer than the schema allows.",
        "Send fewer characters or items.",
    ),
    "pattern": (
        "A string does not match the field's pattern (Rust regex syntax, matched against the whole value).",
        "Match the documented pattern; look-around and back-references are not supported.",
    ),
    "uuid": (
        "Not a canonical UUID (8-4-4-4-12 hexadecimal).",
        "Send the hyphenated form.",
    ),
    "datetime": ("Not an RFC 3339 date-time.", "Use e.g. 2026-11-13T10:00:00Z."),
    "datetime_tz": (
        "The date-time has no UTC offset.",
        "Add Z or an offset such as +01:00.",
    ),
    "finite": ("The number is out of range, NaN or infinite.", "Send a finite number."),
    "too_many_errors": (
        "More than 50 problems; the list was cut.",
        "Fix the first ones and retry.",
    ),
    "depth": ("The body is nested too deeply.", "Flatten the payload."),
    "invalid_json": (
        "The body is not valid JSON (HTTP 400).",
        "Send well-formed JSON (no NaN, comments or trailing commas).",
    ),
    "invalid_form": (
        "The urlencoded form body is malformed or has too many fields (HTTP 400).",
        "Fix the encoding or send fewer than 1000 fields.",
    ),
    "invalid_multipart": (
        "The multipart body is malformed or over a limit (HTTP 400).",
        "Check the boundary, part headers and sizes.",
    ),
    "invalid_content_type": (
        "Several Content-Type values were sent (HTTP 400).",
        "Send exactly one.",
    ),
    "invalid_query": (
        "The query string is malformed (HTTP 400).",
        "Use valid percent-encoding in UTF-8 and at most 1000 parameters.",
    ),
    "unsupported_media_type": (
        "The Content-Type is not one the action accepts (HTTP 415).",
        "Send a type from the action's `parsers=` (JSON by default).",
    ),
    "not_acceptable": (
        "No renderer fits the Accept header (HTTP 406).",
        "Ask for a media type from the action's `renderers=`, or send no Accept.",
    ),
    "payload_too_large": (
        "The body is over the 1 MiB limit (HTTP 413).",
        "Send a smaller body.",
    ),
    "unauthorized": (
        "Missing or invalid credentials (HTTP 401).",
        "Send valid credentials for the action's Access policy.",
    ),
    "forbidden": (
        "Authenticated but not allowed (HTTP 403): missing scope or not the owner.",
        "Use an identity with the required scopes, or act on your own rows.",
    ),
    "not_found": (
        "No such route or row (HTTP 404). Rows hidden by a queryset look the same.",
        "Check the path and identifier.",
    ),
    "method_not_allowed": (
        "The path exists but not for this method (HTTP 405).",
        "See the `Allow` header.",
    ),
    "not_ready": (
        "The worker is starting or shutting down (HTTP 503).",
        "Retry after the `Retry-After` delay.",
    ),
    "overloaded": (
        "The bounded sync executor is full (HTTP 503).",
        "Retry later, or raise `RuntimeConfig.sync_workers` / `sync_queue` if the load is expected.",
    ),
    "timeout": (
        "The request exceeded `RuntimeConfig.request_timeout` (HTTP 504).",
        "Make the action faster or raise the timeout deliberately.",
    ),
    "internal_error": (
        "An unhandled exception (HTTP 500); the body carries a request_id, never the details.",
        "Find the request_id in the server log.",
    ),
}


def explain(code: str) -> tuple[str, str] | None:
    """``(meaning, fix)`` for a known error code."""
    return CATALOG.get(code)
