"""Handwritten-handler fixture for the text-source and renderer measurements.

Same intent as ``phase2_hinoki``: every handler does the least possible work, so
the measurement is the request path around it. It covers ``Form[...]``,
``XML[...]``, ``Query[...]``, the built-in HTML and XML renderers, and a
``Request`` parameter, which Hinoki serves through Python by API.
"""

from hinoki import (
    XML,
    Form,
    Hinoki,
    HTMLRenderer,
    Query,
    Request,
    Schema,
    XMLRenderer,
    http,
)


class TextItem(Schema):
    title: str
    count: int = 0
    flag: bool = False


class Filters(Schema):
    term: str
    limit: int = 20
    flag: bool = False


@http.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@http.post("/form", status=201)
async def create_form(data: Form[TextItem]) -> dict[str, object]:
    return {"title": data.title, "count": data.count, "flag": data.flag}


@http.post("/xml", status=201)
async def create_xml(data: XML[TextItem]) -> dict[str, object]:
    return {"title": data.title, "count": data.count, "flag": data.flag}


@http.get("/query")
async def search(filters: Query[Filters]) -> dict[str, object]:
    return {"term": filters.term, "limit": filters.limit, "flag": filters.flag}


@http.get("/render/html", renderers=[HTMLRenderer()])
async def render_html() -> str:
    return "<p>baseline</p>"


@http.get("/render/xml", renderers=[XMLRenderer(root="report")])
async def render_xml() -> dict[str, object]:
    return {"status": "ok", "count": 3}


@http.get("/request-param")
async def with_request(request: Request) -> dict[str, str]:
    return {"method": request.method}


app = Hinoki(
    include=[
        health,
        create_form,
        create_xml,
        search,
        render_html,
        render_xml,
        with_request,
    ],
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
