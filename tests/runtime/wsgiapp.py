"""An application served through the WSGI adapter."""

from hinoki import Access, Hinoki, MultiPartParser, Request, Schema, UploadedFile, http
from hinoki.parsers import Parsed
from hinoki.wsgi import WSGIAdapter
from tests.providers import TokenAuth

LOG: list[str] = []


class Echo(Schema):
    text: str


class TrackingParser(MultiPartParser):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def parse(self, raw: bytes, content_type: str) -> Parsed:
        self.calls += 1
        return super().parse(raw, content_type)


PROTECTED_UPLOAD_PARSER = TrackingParser()


@http.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "async"}


@http.get("/sync")
def sync_ping() -> dict[str, str]:
    return {"pong": "sync"}


@http.post("/echo", status=201)
async def echo(request: Request, data: Echo) -> Echo:
    return data


@http.post("/upload", parsers=[MultiPartParser(max_part_bytes=16)])
def upload(file: UploadedFile) -> dict[str, object]:
    return {"filename": file.filename, "content": file.content.decode()}


@http.post(
    "/protected-upload",
    access=Access(auth=TokenAuth(), permissions=["posts:write"]),
    parsers=[PROTECTED_UPLOAD_PARSER],
)
def protected_upload(file: UploadedFile) -> dict[str, str]:
    return {"filename": file.filename}


@http.query("/find")
def find(data: Echo) -> Echo:
    return data


def create_app() -> Hinoki:
    app = Hinoki()
    app.include(ping, sync_ping, echo, upload, protected_upload, find)
    return app


wsgi_app = WSGIAdapter(create_app())
