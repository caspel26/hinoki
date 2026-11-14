---
status: implemented
---

# Parsers and renderers

A **parser** turns a request body into Python data; a **renderer** turns a handler's result into response bytes. The built-in ones run in Rust and are the base classes you inherit from.

| Built in | Rust does | You can customise |
| --- | --- | --- |
| `JSONParser` | strict decoding (no NaN, no lone surrogates, depth limit) | `prepare(data)` hook, `media_type` |
| `FormParser` | `application/x-www-form-urlencoded` decoding with a field cap | `max_fields` |
| `MultiPartParser` | `multipart/form-data` (limits, hardening) | part and file limits |
| `TextParser` | UTF-8 text body | `media_type` |
| `JSONRenderer` | encoding of dicts, lists, `Schema`, UUID, datetime, `dump()` objects | `indent`, `sort_keys`, `default`, `prepare(value, request)` hook, `media_type` |
| `HTMLRenderer`, `TextRenderer` | (UTF-8 text) | `media_type` |

Content negotiation (`Accept`, quality values, wildcards) also runs in Rust; with nothing acceptable the answer is 406 before the handler runs.

## Choose them

Per action, or for the whole application (an action's own choice wins):

```python
app = Hinoki(renderers=[JSONRenderer(sort_keys=True), TextRenderer()])

@get("/report", renderers=[JSONRenderer(indent=2)])
async def report() -> dict[str, int]: ...

@post("/hook", parsers=[JSONParser(), TextParser()])
async def hook(request: Request) -> None: ...   # request.data is a dict or a str
```

## Customise without leaving Rust

```python
JSONRenderer(indent=2, sort_keys=True)                 # pretty, stable output
JSONRenderer(default=str)                              # Decimal, Path, ... become text
```

`default` is called for a value the encoder does not know and must return something that is JSON (returning the same object is an error). It does not override what the encoder already handles (UUID, datetime, `Schema`, anything with `dump()`). With `sort_keys=True` object keys are ordered by their encoded text (quotes and escapes included), which is deterministic but not always `json.dumps` order for unusual keys, and integer keys sort as text. `indent` is 0 to 256 (the renderer allows 0 to 16).

## Inherit and override

```python
class Envelope(JSONRenderer):
    media_type = "application/vnd.myapp+json"

    def prepare(self, value, request):                 # reshape, then Rust encodes
        return {"data": value, "request_id": request.request_id}

class SnakeCase(JSONParser):
    def prepare(self, data):                           # Rust decodes, then reshape
        return rename_keys(data) if isinstance(data, dict) else data
```

For a different format write a `Renderer` (`media_type`, `render(value, request) -> bytes`) or a `Parser` (`media_type`, `parse(raw, content_type) -> Parsed`); set `consumes_objects = True` on a renderer that can take `Schema` instances directly. Media types must be distinct within one list; a failing `parse` raises `InvalidBody(code, message)` (a 400 with that stable code).
