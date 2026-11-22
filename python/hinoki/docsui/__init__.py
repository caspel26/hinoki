"""Interactive API documentation: Swagger UI and ReDoc, served from the package.

``Hinoki(openapi_url="/openapi.json", docs_url="/docs", redoc_url="/redoc")``
serves both viewers over the application's own OpenAPI document. The viewer
code is vendored (versions and licences in ``THIRD-PARTY.md``), so documentation
works offline and under a strict ``Content-Security-Policy``: no inline scripts,
no CDN. Pages are plain GETs and are public, like the document they display.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from hinoki import jsontext

STATIC = Path(__file__).parent / "static"
HTML = "text/html; charset=utf-8"
JS = "text/javascript; charset=utf-8"
CSS = "text/css; charset=utf-8"
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "worker-src blob:; frame-ancestors 'none'"
)


@dataclass(frozen=True, slots=True)
class Page:
    body: bytes
    content_type: str
    cacheable: bool


@cache
def asset(name: str) -> str:
    """The served name of a vendored file: its content hash is part of the name, so a
    browser or proxy that cached an older viewer for a day never pairs it with a newer
    page."""
    stem, dot, suffix = name.rpartition(".")
    digest = hashlib.sha256(read(name)).hexdigest()[:10]
    return f"{stem}.{digest}{dot}{suffix}"


def swagger_html(base: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>API documentation</title>"
        f'<link rel="stylesheet" href="{base}/assets/{asset("swagger-ui.css")}"></head>'
        '<body><div id="swagger-ui"></div>'
        f'<script src="{base}/assets/{asset("swagger-ui-bundle.js")}"></script>'
        f'<script src="{base}/assets/init.js"></script></body></html>'
    )


def redoc_html(base: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>API reference</title>"
        "<style>body{margin:0}</style></head><body>"
        '<div id="redoc"></div>'
        f'<script type="module" src="{base}/assets/init.js"></script></body></html>'
    )


def read(name: str) -> bytes:
    return (STATIC / name).read_bytes()


def swagger_pages(base: str, spec: str) -> dict[str, Page]:
    options = f'{{url: {jsontext.dumps(spec)}, dom_id: "#swagger-ui"}}'
    init = f"window.ui = SwaggerUIBundle({options});"
    assets = f"{base}/assets"
    return {
        base: Page(swagger_html(base).encode(), HTML, False),
        f"{assets}/{asset('swagger-ui.css')}": Page(read("swagger-ui.css"), CSS, True),
        f"{assets}/{asset('swagger-ui-bundle.js')}": Page(
            read("swagger-ui-bundle.js"), JS, True
        ),
        f"{assets}/init.js": Page(init.encode(), JS, False),
    }


def redoc_pages(base: str, spec: str) -> dict[str, Page]:
    # ReDoc 3 is an ES module: the page imports its ``init`` instead of reading
    # a ``<redoc>`` element. ReDoc 3 reports usage to a Redocly collector unless
    # ``disableTelemetry`` is set; the viewer must make no outside request.
    init = (
        f'import {{init}} from "./{asset("redoc.standalone.js")}";'
        f"init({jsontext.dumps(spec)}, {{disableTelemetry: true}}, "
        'document.getElementById("redoc"));'
    )
    return {
        base: Page(redoc_html(base).encode(), HTML, False),
        f"{base}/assets/{asset('redoc.standalone.js')}": Page(
            read("redoc.standalone.js"), JS, True
        ),
        f"{base}/assets/init.js": Page(init.encode(), JS, False),
    }


def pages(
    openapi_url: str | None, docs_url: str | None, redoc_url: str | None
) -> dict[str, Page]:
    """Every path the documentation viewers answer, by exact path."""
    found: dict[str, Page] = {}
    if not openapi_url:
        if docs_url or redoc_url:
            raise ValueError("docs_url / redoc_url need openapi_url")
        return found
    if docs_url:
        found.update(swagger_pages(docs_url, openapi_url))
    if redoc_url:
        found.update(redoc_pages(redoc_url, openapi_url))
    return found
