"""Interactive API documentation: Swagger UI and ReDoc, served from the package.

``Hinoki(openapi_url="/openapi.json", docs_url="/docs", redoc_url="/redoc")``
serves both viewers over the application's own OpenAPI document. The viewer
code is vendored (versions and licences in ``THIRD-PARTY.md``), so documentation
works offline and under a strict ``Content-Security-Policy``: no inline scripts,
no CDN. Pages are plain GETs and are public, like the document they display.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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


def swagger_html(base: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>API documentation</title>"
        f'<link rel="stylesheet" href="{base}/assets/swagger-ui.css"></head>'
        '<body><div id="swagger-ui"></div>'
        f'<script src="{base}/assets/swagger-ui-bundle.js"></script>'
        f'<script src="{base}/assets/init.js"></script></body></html>'
    )


def redoc_html(base: str, spec: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>API reference</title>"
        "<style>body{margin:0}</style></head><body>"
        f'<redoc spec-url="{spec}"></redoc>'
        f'<script src="{base}/assets/redoc.standalone.js"></script></body></html>'
    )


def read(name: str) -> bytes:
    return (STATIC / name).read_bytes()


def swagger_pages(base: str, spec: str) -> dict[str, Page]:
    options = f'{{url: {json.dumps(spec)}, dom_id: "#swagger-ui"}}'
    init = f"window.ui = SwaggerUIBundle({options});"
    assets = f"{base}/assets"
    return {
        base: Page(swagger_html(base).encode(), HTML, False),
        f"{assets}/swagger-ui.css": Page(read("swagger-ui.css"), CSS, True),
        f"{assets}/swagger-ui-bundle.js": Page(read("swagger-ui-bundle.js"), JS, True),
        f"{assets}/init.js": Page(init.encode(), JS, False),
    }


def redoc_pages(base: str, spec: str) -> dict[str, Page]:
    return {
        base: Page(redoc_html(base, spec).encode(), HTML, False),
        f"{base}/assets/redoc.standalone.js": Page(
            read("redoc.standalone.js"), JS, True
        ),
    }


def pages(
    openapi_url: str | None, docs_url: str | None, redoc_url: str | None
) -> dict[str, Page]:
    """Every path the documentation viewers answer, by exact path."""
    found: dict[str, Page] = {}
    if (docs_url or redoc_url) and not openapi_url:
        raise ValueError("docs_url / redoc_url need openapi_url")
    if docs_url:
        found.update(swagger_pages(docs_url, openapi_url or ""))
    if redoc_url:
        found.update(redoc_pages(redoc_url, openapi_url or ""))
    return found
