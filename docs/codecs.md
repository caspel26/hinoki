---
status: implemented
---

# Parsers and renderers

A **parser** turns a request body into Python data; a **renderer** turns a handler's result into response bytes. Built-ins use Rust for selected parsing and JSON encoding work; Python still selects codecs, calls customization hooks and assembles framework objects.

| Built in | Rust work | Python work and options |
| --- | --- | --- |
| `JSONParser` | Strict JSON decoding (no NaN, no lone surrogates, depth limit) | `prepare(data)` hook and `media_type` |
| `FormParser` | `application/x-www-form-urlencoded` parsing with a field cap | Builds the Python mappings; `max_fields` |
| `MultiPartParser` | `multipart/form-data` parsing and byte/part limits | Builds `UploadedFile` and form objects; `max_parts`, `max_part_bytes` (each part, including text fields) |
| `TextParser` | — | Decodes UTF-8 in Python; `media_type` |
| `JSONRenderer` | Encodes `dict`, `list`, `Schema`, UUID, datetime and `dump()` objects | `prepare(value, request)` and `default` callbacks run in Python; `indent`, `sort_keys`, `media_type` |
| `HTMLRenderer`, `TextRenderer` | — | Type-check and UTF-8 encode strings in Python; `media_type` |

Content negotiation (`Accept`, quality values, wildcards) also runs in Rust; with nothing acceptable the answer is 406 before the handler runs.

## Upload files

`MultiPartParser` is opt-in. It parses bounded multipart bytes in Rust, then Python creates `UploadedFile` objects. For typed handler binding, annotate a parameter as `UploadedFile`, `UploadedFile | None = None`, or `list[UploadedFile]`; the parameter name selects the multipart field. A single value rejects duplicates, while a list preserves repeated values. The same objects are visible through `request.FILES`; text fields are available in `request.POST`. See [endpoint upload declarations](endpoints.md#upload-files) for a complete example and status-code behavior.

The complete request body and every part are buffered in memory. The default body cap is 1 MiB; increasing it also increases potential memory use across concurrent requests. This parser does not stream or spool files. Keep both the runtime body cap and `max_part_bytes` bounded, and treat filenames and media types as untrusted client input.

## Choose them

Per action, or for the whole application (an action's own choice wins):

```python
app = Hinoki(renderers=[JSONRenderer(sort_keys=True), TextRenderer()])

@http.get("/report", renderers=[JSONRenderer(indent=2)])
async def report() -> dict[str, int]: ...

@http.post("/hook", parsers=[JSONParser(), TextParser()])
async def hook(request: Request) -> None: ...   # request.data is a dict or a str
```

## Configure the Rust JSON encoder

```python
JSONRenderer(indent=2, sort_keys=True)                 # pretty, stable output
JSONRenderer(default=str)                              # Decimal, Path, ... become text
```

`default` is a Python callback for a value the encoder does not know and must return something that is JSON (returning the same object is an error). It does not override what the encoder already handles (UUID, datetime, `Schema`, anything with `dump()`). With `sort_keys=True` object keys are ordered by their encoded text (quotes and escapes included), which is deterministic but not always `json.dumps` order for unusual keys, and integer keys sort as text. The public renderer accepts `indent` from 0 to 16, or `None` for compact output.

## Inherit and override

```python
from hinoki import JSONParser, JSONRenderer


class Envelope(JSONRenderer):
    media_type = "application/vnd.myapp+json"

    def prepare(self, value, request):                 # reshape, then Rust encodes
        return {"data": value, "request_id": request.request_id}

class SnakeCase(JSONParser):
    def prepare(self, data):                           # Rust decodes, then reshape
        return rename_keys(data) if isinstance(data, dict) else data
```

For a different format write a `Renderer` (`media_type`, `render(value, request) -> bytes`) or a `Parser` (`media_type`, `parse(raw, content_type) -> Parsed`); set `consumes_objects = True` on a renderer that can take `Schema` instances directly. Media types must be distinct within one list; a failing `parse` raises `InvalidBody(code, message)` (a 400 with that stable code).
